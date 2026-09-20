using System;
using System.IO;
using NAudio.Wave;

internal static class AudioDecode
{
    [MTAThread]
    static int Main(string[] args)
    {
        if (args.Length != 2 || !File.Exists(args[0])) return 2;
        try
        {
            // Decode the chosen local file. No playback or capture device is opened.
            using (var reader = new MediaFoundationReader(Path.GetFullPath(args[0])))
            using (var resampler = new MediaFoundationResampler(reader, new WaveFormat(16000, 16, 1)))
            {
                resampler.ResamplerQuality = 60;
                WaveFileWriter.CreateWaveFile(Path.GetFullPath(args[1]), resampler);
            }
            return 0;
        }
        catch (DllNotFoundException) { return 3; }
        catch (Exception) { return 1; }
    }
}
