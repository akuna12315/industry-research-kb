// A CLR executable starts without self-extracting into Windows TEMP.
// Only its child installer receives the private writable TEMP/TMP values.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("研知安装程序")]
[assembly: AssemblyVersion("0.1.5.0")]
[assembly: AssemblyFileVersion("0.1.5.0")]

internal static class SetupBootstrap
{
    private static string Quote(string value)
    {
        // Windows CommandLineToArgvW escaping; preserve all installer options.
        StringBuilder output = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') { output.Append('\\', slashes * 2 + 1); output.Append(ch); }
            else { output.Append('\\', slashes); output.Append(ch); }
            slashes = 0;
        }
        output.Append('\\', slashes * 2);
        output.Append('"');
        return output.ToString();
    }

    private static string WritableCache()
    {
        string executableDirectory = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string profileDirectory = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string[] candidates = {
            String.IsNullOrEmpty(profileDirectory) ? null : Path.Combine(profileDirectory, "YanZhi", "InstallerTemp"),
            Path.Combine(executableDirectory, ".yanzhi-installer-temp")
        };
        foreach (string candidate in candidates)
        {
            if (String.IsNullOrEmpty(candidate)) continue;
            try
            {
                string basePath = Path.GetFullPath(candidate);
                Directory.CreateDirectory(basePath);
                string probe = Path.Combine(basePath, "write-check-" + Guid.NewGuid().ToString("N"));
                using (FileStream stream = new FileStream(probe, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    stream.WriteByte(1);
                File.Delete(probe);
                return basePath;
            }
            catch (UnauthorizedAccessException) { }
            catch (IOException) { }
        }
        throw new IOException("没有可写的安装缓存目录。请将安装包放到自己可写的文件夹后重试。");
    }

    private static void ExtractVerified(string destination)
    {
        Assembly assembly = Assembly.GetExecutingAssembly();
        using (Stream embedded = assembly.GetManifestResourceStream("YanZhi.Setup.Payload"))
        using (FileStream output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        {
            if (embedded == null) throw new InvalidDataException("安装文件缺失。");
            embedded.CopyTo(output);
        }
        string expected;
        using (Stream embeddedHash = assembly.GetManifestResourceStream("YanZhi.Setup.SHA256"))
        using (StreamReader reader = new StreamReader(embeddedHash, Encoding.ASCII))
            expected = reader.ReadToEnd().Trim();
        using (SHA256 sha = SHA256.Create())
        using (FileStream payload = File.OpenRead(destination))
        {
            string actual = BitConverter.ToString(sha.ComputeHash(payload)).Replace("-", "");
            if (!String.Equals(expected, actual, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("安装文件校验失败，请重新取得安装包。");
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        string runDirectory = null;
        string cache = null;
        try
        {
            cache = WritableCache();
            runDirectory = Path.Combine(cache, "run-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(runDirectory);
            string payload = Path.Combine(runDirectory, "setup-inner.exe");
            ExtractVerified(payload);
            string[] quoted = Array.ConvertAll(args, Quote);
            string options = String.Join(" ", quoted);
            ProcessStartInfo start = new ProcessStartInfo(payload, options);
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WorkingDirectory = runDirectory;
            start.EnvironmentVariables["TEMP"] = cache;
            start.EnvironmentVariables["TMP"] = cache;
            using (Process process = Process.Start(start))
            {
                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            bool silent = Array.Exists(args, delegate(string arg) {
                return String.Equals(arg, "/VERYSILENT", StringComparison.OrdinalIgnoreCase)
                    || String.Equals(arg, "/SILENT", StringComparison.OrdinalIgnoreCase);
            });
            string message = "安装程序未能启动：" + error.Message;
            // Never log command-line arguments (they may contain user passwords).
            if (cache != null)
            {
                try { File.WriteAllText(Path.Combine(cache, "bootstrap-error.txt"), message, Encoding.UTF8); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
            if (!silent) MessageBox.Show(message, "研知安装程序", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            // Delete only the unique directory this invocation created, never its parent.
            if (runDirectory != null && cache != null)
            {
                try
                {
                    string full = Path.GetFullPath(runDirectory);
                    string parent = Path.GetFullPath(cache).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                    if (full.StartsWith(parent, StringComparison.OrdinalIgnoreCase)
                        && !String.Equals(full.TrimEnd(Path.DirectorySeparatorChar), parent.TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase)
                        && Directory.Exists(full)
                        && (File.GetAttributes(full) & FileAttributes.ReparsePoint) == 0)
                        Directory.Delete(full, true);
                }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }
}
