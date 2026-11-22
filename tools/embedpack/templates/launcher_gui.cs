using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

[assembly: AssemblyTitle("embedpack windowed launcher")]
[assembly: AssemblyProduct("embedpack")]
[assembly: AssemblyFileVersion("1.2.0.0")]

// Windowed launcher: <exedir>\pythonw.exe runs <exedir>\app\__main__.pyc;
// child stdout/stderr are discarded.
class Launcher {
    static string Quote(string s) {
        StringBuilder sb = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in s) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') {
                sb.Append('\\', slashes * 2 + 1);
                sb.Append('"');
                slashes = 0;
                continue;
            }
            sb.Append('\\', slashes);
            slashes = 0;
            sb.Append(c);
        }
        if (slashes > 0) sb.Append('\\', slashes * 2);
        sb.Append('"');
        return sb.ToString();
    }

    static void Log(string msg) {
        try {
            string logPath = Assembly.GetExecutingAssembly().Location + ".log";
            File.AppendAllText(logPath,
                DateTime.UtcNow.ToString("u") + "  " + msg + Environment.NewLine,
                Encoding.UTF8);
        } catch (Exception) {
        }
    }

    static int Main(string[] args) {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string pyExe = Path.Combine(dir, "pythonw.exe");
        string entry = Path.Combine(dir, "app", "__main__.pyc");

        if (!File.Exists(pyExe)) {
            Log("runtime not found: " + pyExe);
            return 101;
        }
        if (!File.Exists(entry)) {
            Log("entry not found: " + entry);
            return 102;
        }

        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = pyExe;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.Arguments = Quote(entry);
        foreach (string a in args) {
            psi.Arguments += " " + Quote(a);
        }

        try {
            using (Process p = Process.Start(psi)) {
                p.WaitForExit();
                return p.ExitCode;
            }
        } catch (Exception ex) {
            Log("failed to start runtime: " + ex.Message);
            return 103;
        }
    }
}
