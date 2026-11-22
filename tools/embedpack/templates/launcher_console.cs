using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

[assembly: AssemblyTitle("embedpack console launcher")]
[assembly: AssemblyProduct("embedpack")]
[assembly: AssemblyFileVersion("1.1.0.0")]

// Console launcher: <exedir>\python.exe runs <exedir>\app\__main__.pyc.
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

    static int Main(string[] args) {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string pyExe = Path.Combine(dir, "python.exe");
        string entry = Path.Combine(dir, "app", "__main__.pyc");

        if (!File.Exists(pyExe)) {
            Console.Error.WriteLine("runtime not found: " + pyExe);
            return 101;
        }
        if (!File.Exists(entry)) {
            Console.Error.WriteLine("entry not found: " + entry);
            return 102;
        }

        // Ctrl+C belongs to the child; this shell keeps waiting.
        Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) {
            e.Cancel = true;
        };

        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = pyExe;
        psi.UseShellExecute = false;
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
            Console.Error.WriteLine("failed to start runtime: " + ex.Message);
            return 103;
        }
    }
}
