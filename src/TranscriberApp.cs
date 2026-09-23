using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        bool created;
        using (var mutex = new Mutex(true, "Local\\DoubaoFileTranscriber", out created))
        {
            if (!created) { MessageBox.Show("程序已经在运行，请双击托盘图标打开后拖入录音。", "豆包录音转写"); return; }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new TranscriberForm(args));
        }
    }
}

internal sealed class WorkItem
{
    public string Source;
    public string Output;
    public string OutputFolder;
    public int Workers;
    public ListViewItem Row;
}

internal sealed class TranscriberForm : Form
{
    readonly string root = AppDomain.CurrentDomain.BaseDirectory;
    readonly Queue<WorkItem> queue = new Queue<WorkItem>();
    readonly HashSet<string> pending = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly ListView files = new ListView();
    readonly Label hint = new Label();
    readonly Label folderLabel = new Label();
    readonly ProgressBar progress = new ProgressBar();
    readonly NotifyIcon tray = new NotifyIcon();
    readonly CheckBox autoHide = new CheckBox();
    readonly Button cancel = new Button();
    readonly Label compatibilityLabel = new Label();
    readonly Button recheck = new Button();
    readonly Button website = new Button();
    readonly NumericUpDown workerSelector = new NumericUpDown();
    string outputFolder;
    Process activeProcess;
    IntPtr activeJob;
    bool busy, quitting, cancelled, ready;

    public TranscriberForm(string[] args)
    {
        Text = "豆包录音转写";
        Size = new Size(880, 680);
        MinimumSize = new Size(820, 600);
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Microsoft YaHei UI", 10);
        BackColor = Color.FromArgb(247, 249, 252);
        Icon = SystemIcons.Application;
        outputFolder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "豆包转写结果");

        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(22), RowCount = 8, ColumnCount = 1 };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 46));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 50));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 92));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 32));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 28));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 38));
        Controls.Add(layout);
        var title = new Label { Text = "把录音拖到这里，自动保存为 TXT", Font = new Font(Font.FontFamily, 17, FontStyle.Bold), Dock = DockStyle.Fill };
        layout.Controls.Add(title, 0, 0);
        var subtitle = new Label { Text = "可一次加入多个文件。识别时可以正常使用电脑；关闭窗口后仍在托盘运行。\n使用本机豆包输入法的云端识别能力，需要联网。", Dock = DockStyle.Fill, ForeColor = Color.DimGray };
        layout.Controls.Add(subtitle, 0, 1);
        var compatibilityPanel = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1 };
        compatibilityPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, 52));
        compatibilityPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, 38));
        compatibilityLabel.Text = "正在检测豆包输入法…";
        compatibilityLabel.Dock = DockStyle.Fill;
        compatibilityPanel.Controls.Add(compatibilityLabel, 0, 0);
        var compatibilityActions = new FlowLayoutPanel { Dock = DockStyle.Fill };
        recheck.Text = "重新检测"; recheck.AutoSize = true;
        recheck.Click += async delegate { await CheckCompatibility(); };
        website.Text = "打开豆包输入法官网"; website.AutoSize = true;
        website.Click += delegate { Process.Start(new ProcessStartInfo("https://ime.doubao.com/") { UseShellExecute = true }); };
        compatibilityActions.Controls.AddRange(new Control[] { recheck, website });
        compatibilityPanel.Controls.Add(compatibilityActions, 0, 1);
        layout.Controls.Add(compatibilityPanel, 0, 2);
        files.View = View.Details;
        files.FullRowSelect = true;
        files.GridLines = false;
        files.ShowItemToolTips = true;
        files.Dock = DockStyle.Fill;
        files.Columns.Add("录音文件", 370);
        files.Columns.Add("状态", 350);
        files.DoubleClick += delegate { OpenSelected(); };
        layout.Controls.Add(files, 0, 3);
        var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(0, 5, 0, 0) };
        var add = new Button { Text = "添加录音", AutoSize = true };
        add.Click += delegate {
            using (var dialog = new OpenFileDialog { Multiselect = true, Filter = "音频 / 视频|*.wav;*.mp3;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.wma;*.mp4;*.webm;*.amr;*.mkv;*.mov|所有文件|*.*" })
                if (dialog.ShowDialog(this) == DialogResult.OK) AddPaths(dialog.FileNames);
        };
        var choose = new Button { Text = "选择保存目录", AutoSize = true };
        choose.Click += delegate {
            using (var dialog = new FolderBrowserDialog { SelectedPath = outputFolder, Description = "选择 TXT 保存目录（对之后加入的文件生效）" })
                if (dialog.ShowDialog(this) == DialogResult.OK) { outputFolder = dialog.SelectedPath; UpdateFolder(); }
        };
        var open = new Button { Text = "打开结果目录", AutoSize = true };
        open.Click += delegate { Directory.CreateDirectory(outputFolder); Process.Start(new ProcessStartInfo(outputFolder) { UseShellExecute = true }); };
        cancel.Text = "停止当前任务";
        cancel.AutoSize = true;
        cancel.Enabled = false;
        cancel.Click += delegate { StopCurrent(); };
        var workerLabel = new Label { Text = "并发任务池", AutoSize = true, Margin = new Padding(12, 5, 4, 0) };
        workerSelector.Minimum = 1;
        workerSelector.Maximum = 20;
        workerSelector.Value = 5;
        workerSelector.Width = 52;
        workerSelector.Margin = new Padding(0, 2, 0, 0);
        buttons.Controls.AddRange(new Control[] { add, choose, open, cancel, workerLabel, workerSelector });
        layout.Controls.Add(buttons, 0, 4);
        folderLabel.Dock = DockStyle.Fill;
        folderLabel.AutoEllipsis = true;
        folderLabel.ForeColor = Color.DimGray;
        layout.Controls.Add(folderLabel, 0, 5);
        progress.Dock = DockStyle.Fill;
        layout.Controls.Add(progress, 0, 6);
        var bottom = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(0, 6, 0, 0) };
        hint.Text = "等待录音 · 双击已完成的文件打开 TXT";
        hint.AutoSize = true;
        autoHide.Text = "加入后隐藏到托盘";
        autoHide.AutoSize = true;
        var clearRecovery = new Button { Text = "清除恢复缓存", AutoSize = true };
        clearRecovery.Click += delegate { ClearRecoveryCache(); };
        bottom.Controls.Add(autoHide);
        bottom.Controls.Add(clearRecovery);
        bottom.Controls.Add(hint);
        layout.Controls.Add(bottom, 0, 7);
        UpdateFolder();
        AttachDrop(this);

        tray.Icon = SystemIcons.Application;
        tray.Text = "豆包录音转写 · 等待录音";
        tray.Visible = true;
        tray.DoubleClick += delegate { ShowWindow(); };
        var menu = new ContextMenuStrip();
        menu.Items.Add("显示转写窗口", null, delegate { ShowWindow(); });
        menu.Items.Add("打开结果目录", null, delegate { Directory.CreateDirectory(outputFolder); Process.Start(new ProcessStartInfo(outputFolder) { UseShellExecute = true }); });
        menu.Items.Add("退出", null, delegate {
            if (busy && MessageBox.Show("退出会停止当前任务并清空等待队列。确定退出？", Text, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
            quitting = true; StopCurrent(); Close();
        });
        tray.ContextMenuStrip = menu;
        FormClosing += delegate(object sender, FormClosingEventArgs e) {
            if (!quitting && e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; Hide(); }
            else { StopCurrent(); tray.Visible = false; tray.Dispose(); }
        };
        Resize += delegate { if (WindowState == FormWindowState.Minimized) Hide(); };
        Shown += async delegate { await CheckCompatibility(); if (args.Length > 0) AddPaths(args); };
    }

    ProcessStartInfo BackendInfo(string arguments)
    {
        return new ProcessStartInfo {
            FileName = Path.Combine(root, "runtime", "python.exe"),
            Arguments = "-I -B " + Quote(Path.Combine(root, "app", "doubao_transcribe.py")) + " " + arguments,
            WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
        };
    }

    async Task CheckCompatibility()
    {
        if (busy) return;
        ready = false; recheck.Enabled = false;
        compatibilityLabel.Text = "正在检测豆包输入法…";
        try
        {
            using (var process = new Process { StartInfo = BackendInfo("--check") })
            {
                process.Start();
                Task<string> output = process.StandardOutput.ReadToEndAsync();
                Task<string> error = process.StandardError.ReadToEndAsync();
                bool exited = await Task.Run(() => process.WaitForExit(20000));
                if (!exited) { process.Kill(); throw new TimeoutException(); }
                string response = await output;
                await error;
                if (process.ExitCode != 0) throw new InvalidOperationException();
                var result = json.Deserialize<Dictionary<string, object>>(response.Trim());
                ready = Convert.ToString(result["status"]) == "ready";
                compatibilityLabel.Text = Convert.ToString(result["message"]);
                compatibilityLabel.ForeColor = ready ? Color.FromArgb(24, 120, 70) : Color.Firebrick;
                website.Visible = !ready;
            }
        }
        catch { compatibilityLabel.Text = "检测组件无法启动。请重新安装本工具；支持 Windows 10/11 64 位系统。"; compatibilityLabel.ForeColor = Color.Firebrick; }
        finally { recheck.Enabled = true; }
        if (ready && queue.Count > 0 && !busy) RunNext();
    }

    void AttachDrop(Control control)
    {
        control.AllowDrop = true;
        control.DragEnter += delegate(object sender, DragEventArgs e) { e.Effect = e.Data.GetDataPresent(DataFormats.FileDrop) ? DragDropEffects.Copy : DragDropEffects.None; };
        control.DragDrop += delegate(object sender, DragEventArgs e) { var paths = e.Data.GetData(DataFormats.FileDrop) as string[]; if (paths != null) AddPaths(paths); };
        foreach (Control child in control.Controls) AttachDrop(child);
    }

    void UpdateFolder() { folderLabel.Text = "保存到：" + outputFolder + "  ·  同名文件不会覆盖"; }
    void ShowWindow() { Show(); WindowState = FormWindowState.Normal; Activate(); }

    void ClearRecoveryCache()
    {
        if (busy) { MessageBox.Show("请先停止当前任务，再清除恢复缓存。", Text); return; }
        string folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                     "DoubaoFileTranscriber", "Checkpoints");
        if (!Directory.Exists(folder)) { MessageBox.Show("没有待清除的恢复缓存。", Text); return; }
        try {
            var records = new List<string>();
            records.AddRange(Directory.GetFiles(folder, "checkpoint-*.json"));
            records.AddRange(Directory.GetFiles(folder, "checkpoint-*.tmp"));
            if (records.Count == 0) { MessageBox.Show("没有待清除的恢复缓存。", Text); return; }
            if (MessageBox.Show("清除后，未完成录音的已识别分段将无法恢复，需要重新转写。确定清除？",
                                Text, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
            foreach (string path in records) File.Delete(path);
            hint.Text = "已清除 " + records.Count + " 个恢复缓存文件";
        } catch (Exception ex) {
            MessageBox.Show("清除失败：" + ex.GetType().Name + "。请关闭其他转写任务后重试。", Text);
        }
    }

    void OpenSelected()
    {
        if (files.SelectedItems.Count == 0) return;
        var work = files.SelectedItems[0].Tag as WorkItem;
        if (work != null && work.Output != null && File.Exists(work.Output))
            Process.Start(new ProcessStartInfo(work.Output) { UseShellExecute = true });
    }

    void AddPaths(string[] paths)
    {
        foreach (string raw in paths)
        {
            string path;
            try { path = Path.GetFullPath(raw); } catch { continue; }
            if (!File.Exists(path) || pending.Contains(path)) continue;
            var work = new WorkItem { Source = path, OutputFolder = outputFolder, Workers = (int)workerSelector.Value };
            work.Row = new ListViewItem(new string[] { Path.GetFileName(path), "排队中 · " + work.Workers + " 路" });
            work.Row.Tag = work;
            work.Row.ToolTipText = path;
            files.Items.Add(work.Row);
            queue.Enqueue(work);
            pending.Add(path);
        }
        if (autoHide.Checked && queue.Count > 0) Hide();
        if (!busy) RunNext();
    }

    static string Quote(string value)
    {
        var result = new StringBuilder("\""); int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            result.Append(c); slashes = 0;
        }
        result.Append('\\', slashes * 2); result.Append('"'); return result.ToString();
    }

    async void RunNext()
    {
        if (queue.Count == 0 || quitting || !ready) { busy = false; cancel.Enabled = false; return; }
        busy = true;
        cancelled = false;
        cancel.Enabled = true;
        progress.Value = 0;
        WorkItem work = queue.Dequeue();
        work.Row.SubItems[1].Text = "准备中 · " + work.Workers + " 路";
        hint.Text = "后台转写中 · " + work.Workers + " 路 · 等待 " + queue.Count + " 个";
        tray.Text = "豆包录音转写 · 正在识别";
        bool completed = false;
        string failure = null;
        string cacheRoot = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "DoubaoFileTranscriber", "Cache");
        string taskCache = Path.Combine(cacheRoot, Guid.NewGuid().ToString("N"));
        try
        {
            var info = BackendInfo(Quote(work.Source) + " --output-dir " + Quote(work.OutputFolder) + " --work-dir " + Quote(taskCache) + " --workers " + work.Workers);
            using (var process = new Process { StartInfo = info })
            {
                activeProcess = process;
                process.Start();
                activeJob = NativeJob.Create(process);
                Task<string> stderr = process.StandardError.ReadToEndAsync();
                string line;
                while ((line = await process.StandardOutput.ReadLineAsync()) != null)
                {
                    Dictionary<string, object> update;
                    try { update = json.Deserialize<Dictionary<string, object>>(line); } catch { continue; }
                    string type = update.ContainsKey("type") ? Convert.ToString(update["type"]) : "";
                    if (update.ContainsKey("message")) work.Row.SubItems[1].Text = Convert.ToString(update["message"]);
                    if (update.ContainsKey("percent")) progress.Value = Math.Max(0, Math.Min(100, Convert.ToInt32(update["percent"])));
                    if (update.ContainsKey("output")) work.Output = Convert.ToString(update["output"]);
                    if (type == "done")
                    {
                        completed = true;
                        work.Row.ForeColor = Color.FromArgb(24, 120, 70);
                        string cacheWarning = update.ContainsKey("cache_warning") ? Convert.ToString(update["cache_warning"]) : "";
                        if (!String.IsNullOrEmpty(cacheWarning))
                        {
                            work.Row.SubItems[1].Text = "完成 · 临时缓存未清理";
                            work.Row.ToolTipText = cacheWarning;
                            tray.ShowBalloonTip(5000, Text, "转写已完成，但临时缓存未清理。请在列表中查看路径。", ToolTipIcon.Warning);
                        }
                        else if (update.ContainsKey("warnings") && json.Serialize(update["warnings"]) != "[]")
                            work.Row.SubItems[1].Text = "完成 · 连续长句分段处请校对";
                    }
                    if (type == "error") failure = Convert.ToString(update["message"]);
                }
                await Task.Run(() => process.WaitForExit());
                await stderr; // Drain privately; SDK/runtime logs are not shown or saved.
                if (process.ExitCode != 0 && failure == null) failure = "后台进程已停止";
            }
        }
        catch (Exception ex) { StopCurrent(); failure = "无法启动或读取后台程序：" + ex.GetType().Name; }
        finally
        {
            activeProcess = null;
            if (activeJob != IntPtr.Zero) { NativeJob.CloseHandle(activeJob); activeJob = IntPtr.Zero; }
            try { if (Directory.Exists(taskCache)) Directory.Delete(taskCache, true); } catch { }
            pending.Remove(work.Source);
        }
        if (quitting || IsDisposed) return;
        if (cancelled) { work.Row.SubItems[1].Text = "已停止"; work.Row.ForeColor = Color.DimGray; }
        else if (!completed || failure != null) { work.Row.SubItems[1].Text = failure ?? "未收到完成结果"; work.Row.ForeColor = Color.Firebrick; }
        hint.Text = completed && !cancelled ? "已保存 TXT · 双击结果可打开" : "任务已结束 · 可再次加入同一文件继续";
        tray.Text = "豆包录音转写 · " + (completed ? "已完成" : "任务已结束");
        busy = false;
        RunNext();
    }

    void StopCurrent()
    {
        if (activeProcess == null) return;
        cancelled = true;
        try {
            if (activeJob != IntPtr.Zero) NativeJob.TerminateJobObject(activeJob, 1);
            else if (!activeProcess.HasExited) activeProcess.Kill();
        } catch { }
    }
}

internal static class NativeJob
{
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits { public long PerProcess, PerJob; public uint Flags; public UIntPtr MinWorkingSet, MaxWorkingSet; public uint ActiveProcesses; public UIntPtr Affinity; public uint Priority, Scheduling; }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits { public BasicLimits Basic; public IoCounters Io; public UIntPtr ProcessMemory, JobMemory, PeakProcess, PeakJob; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int kind, IntPtr info, uint size);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] public static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
    public static IntPtr Create(Process process)
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        var limits = new ExtendedLimits(); limits.Basic.Flags = 0x2000;
        int size = Marshal.SizeOf(limits);
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try {
            Marshal.StructureToPtr(limits, buffer, false);
            if (job == IntPtr.Zero || !SetInformationJobObject(job, 9, buffer, (uint)size) || !AssignProcessToJobObject(job, process.Handle)) {
                if (job != IntPtr.Zero) CloseHandle(job);
                throw new InvalidOperationException("Could not isolate background process lifetime.");
            }
            return job;
        } finally { Marshal.FreeHGlobal(buffer); }
    }
}
