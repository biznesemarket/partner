// VS Code-обертка partner-agent. В телеметрию уходят только счетчики
// активности; текст документа, промпты и имена файлов не отправляются.
const cp = require("child_process");
const vscode = require("vscode");

let statusItem;
let timer;
let sessionActive = false;
let counters = { input_events: 0, focus: 1, tool_calls: 0, files_touched: 0 };

function cliPath() {
  return vscode.workspace.getConfiguration("partner").get("cliPath", "partner-agent");
}

function runCli(args) {
  const cli = cliPath();
  return new Promise((resolve, reject) => {
    cp.execFile(cli, args, { timeout: 20000 }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error((stderr || error.message || "").trim()));
        return;
      }
      resolve((stdout || "").trim());
    });
  });
}

function setStatus(text) {
  if (statusItem) {
    statusItem.text = `$(clock) Партнер: ${text}`;
    statusItem.show();
  }
}

function workspaceRoot() {
  const folders = vscode.workspace.workspaceFolders;
  return folders && folders.length ? folders[0].uri.fsPath : undefined;
}

async function currentBranch() {
  try {
    return await new Promise((resolve) => {
      cp.execFile("git", ["rev-parse", "--abbrev-ref", "HEAD"], { cwd: workspaceRoot() }, (error, stdout) => {
        resolve(error ? "" : String(stdout || "").trim());
      });
    });
  } catch {
    return "";
  }
}

async function startSession() {
  const task = await vscode.window.showInputBox({
    prompt: "Задача/issue для привязки сессии",
    placeHolder: "например, 42 или T-17",
  });
  if (task === undefined) {
    return;
  }
  const branch = await currentBranch();
  const args = ["start", "--task", task];
  if (branch) {
    args.push("--branch", branch);
  }
  try {
    const out = await runCli(args);
    sessionActive = true;
    setStatus("сессия идет");
    vscode.window.showInformationMessage(out.split("\n")[0] || "Сессия начата");
    startHeartbeat();
  } catch (error) {
    vscode.window.showErrorMessage(`Партнер: ${error.message}`);
  }
}

async function stopSession() {
  stopHeartbeat();
  try {
    await runCli(["stop"]);
    sessionActive = false;
    setStatus("сессия завершена");
    vscode.window.showInformationMessage("Сессия завершена, итог дня — partner-agent day <дата>");
  } catch (error) {
    vscode.window.showErrorMessage(`Партнер: ${error.message}`);
  }
}

async function showStatus() {
  try {
    const out = await runCli(["status"]);
    vscode.window.showInformationMessage(out.split("\n").slice(0, 4).join(" "));
  } catch (error) {
    vscode.window.showErrorMessage(`Партнер: ${error.message}`);
  }
}

function startHeartbeat() {
  stopHeartbeat();
  const seconds = Math.max(30, vscode.workspace.getConfiguration("partner").get("heartbeatSec", 30));
  timer = setInterval(async () => {
    if (!sessionActive) {
      return;
    }
    const args = [
      "heartbeat",
      "--input", String(counters.input_events),
      "--focus", String(counters.focus),
      "--tool-calls", String(counters.tool_calls),
      "--files-touched", String(counters.files_touched),
      "--idle-sec", String(counters.input_events === 0 && counters.tool_calls === 0 ? seconds : 0),
      "--agent-running",
    ];
    counters = { input_events: 0, focus: 1, tool_calls: 0, files_touched: 0 };
    try {
      await runCli(args);
      setStatus("сессия идет");
    } catch (error) {
      setStatus("офлайн-буфер");
    }
  }, seconds * 1000);
}

function stopHeartbeat() {
  if (timer) {
    clearInterval(timer);
    timer = undefined;
  }
}

function activate(context) {
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusItem.command = "partner.status";
  setStatus("нет сессии");

  context.subscriptions.push(
    vscode.commands.registerCommand("partner.startSession", startSession),
    vscode.commands.registerCommand("partner.stopSession", stopSession),
    vscode.commands.registerCommand("partner.status", showStatus),
    vscode.workspace.onDidChangeTextDocument(() => {
      counters.input_events += 1;
      counters.focus = 1;
    }),
    vscode.window.onDidChangeWindowState((state) => {
      counters.focus = state.focused ? 1 : 0;
    }),
    statusItem
  );
}

function deactivate() {
  stopHeartbeat();
}

module.exports = { activate, deactivate };
