use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{
    atomic::{AtomicU64, Ordering},
    LazyLock, Mutex,
};
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use sysinfo::{ProcessRefreshKind, RefreshKind, System};
use tauri::{command, AppHandle, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

use crate::logging::{log_sync, redact_secrets};
use crate::storage::{daemon_launch_path, read_json, write_json, DaemonLaunchConfig};

const DEFAULT_PORT_RANGE: (u16, u16) = (8765, 8799);
const PORT_PROBE_TIMEOUT_MS: u64 = 100;
const PROCESS_TERMINATION_RETRIES: u32 = 20;
const PROCESS_TERMINATION_DELAY_MS: u64 = 100;

// Windows-specific flag: do not create a console window for the child process.
// This applies both to the daemon executable and to the taskkill helper.
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

/// Configure a Command so it does not spawn a visible console window on Windows.
/// On other platforms this is a no-op.
#[cfg(target_os = "windows")]
fn suppress_console_window(cmd: &mut Command) {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(target_os = "windows"))]
fn suppress_console_window(_cmd: &mut Command) {}

/// Status returned to the frontend describing the local daemon.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DaemonStatus {
    pub running: bool,
    pub pid: Option<u32>,
    pub port: Option<u16>,
    pub url: Option<String>,
    pub executable: Option<String>,
    pub last_error: Option<String>,
}

/// In-memory handle to the active daemon process.
#[derive(Debug, Clone)]
struct DaemonHandle {
    pid: u32,
    port: u16,
    executable: String,
}

static DAEMON_HANDLES: LazyLock<Mutex<HashMap<String, DaemonHandle>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));
static DAEMON_START_LOCK: LazyLock<Mutex<()>> = LazyLock::new(|| Mutex::new(()));
static WORKSPACE_WINDOW_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone, Default, Deserialize)]
pub struct OpenWorkspaceWindowOptions {
    pub session_id: Option<String>,
    pub view: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct OpenWorkspaceWindowResult {
    pub label: String,
    pub daemon: DaemonStatus,
}

fn daemon_handle(label: &str) -> Result<Option<DaemonHandle>, String> {
    DAEMON_HANDLES
        .lock()
        .map_err(|_| "守护进程状态锁被污染".to_string())
        .map(|handles| handles.get(label).cloned())
}

fn store_daemon_handle(label: &str, handle: DaemonHandle) -> Result<(), String> {
    DAEMON_HANDLES
        .lock()
        .map_err(|_| "守护进程状态锁被污染".to_string())?
        .insert(label.to_string(), handle);
    Ok(())
}

fn take_daemon_handle(label: &str) -> Result<Option<DaemonHandle>, String> {
    DAEMON_HANDLES
        .lock()
        .map_err(|_| "守护进程状态锁被污染".to_string())
        .map(|mut handles| handles.remove(label))
}

fn encode_query_component(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            encoded.push(char::from(byte));
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

fn workspace_window_label() -> String {
    let sequence = WORKSPACE_WINDOW_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    format!("workspace-{}-{sequence}", std::process::id())
}

fn workspace_window_url(
    label: &str,
    api: &str,
    view: Option<&str>,
    session_id: Option<&str>,
) -> PathBuf {
    let mut query = format!(
        "index.html?naumiWindow={}&naumiApi={}&naumiView={}",
        encode_query_component(label),
        encode_query_component(api),
        encode_query_component(view.unwrap_or("web2")),
    );
    if let Some(session_id) = session_id {
        query.push_str("&naumiSession=");
        query.push_str(&encode_query_component(session_id));
    }
    PathBuf::from(query)
}

/// Probe whether a local TCP port is currently reachable (occupied).
fn is_port_reachable(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let timeout = Duration::from_millis(PORT_PROBE_TIMEOUT_MS);
    TcpStream::connect_timeout(&addr, timeout).is_ok()
}

/// Find the first available port in the configured range.
fn find_available_port(range: (u16, u16)) -> Option<u16> {
    let tracked: HashSet<u16> = DAEMON_HANDLES
        .lock()
        .ok()
        .map(|handles| handles.values().map(|handle| handle.port).collect())
        .unwrap_or_default();
    for port in range.0..=range.1 {
        if !tracked.contains(&port) && !is_port_reachable(port) {
            return Some(port);
        }
    }
    None
}

/// Build the executable and argument list for launching the daemon.
fn build_daemon_command(
    config: &DaemonLaunchConfig,
    port: u16,
) -> Result<(String, Vec<String>), String> {
    let executable = config
        .executable
        .clone()
        .unwrap_or_else(|| "naumi".to_string());

    let is_python = executable.eq_ignore_ascii_case("python")
        || executable.eq_ignore_ascii_case("python.exe")
        || executable.to_lowercase().ends_with("python.exe");

    let args: Vec<String> = if config.args.is_empty() {
        if is_python {
            vec![
                "-m".to_string(),
                "naumi_agent".to_string(),
                "serve".to_string(),
                "--port".to_string(),
                port.to_string(),
            ]
        } else {
            vec!["serve".to_string(), "--port".to_string(), port.to_string()]
        }
    } else {
        config
            .args
            .iter()
            .map(|arg| arg.replace("{port}", &port.to_string()))
            .collect()
    };

    Ok((executable, args))
}

/// Search for a `.venv` directory starting from the current working directory
/// and walking up to the drive root. Returns the path to the venv python if found.
fn find_venv_python() -> Option<std::path::PathBuf> {
    let mut current = std::env::current_dir().ok()?;
    loop {
        let candidate = current.join(".venv").join("Scripts").join("python.exe");
        if candidate.exists() {
            return Some(candidate);
        }
        if !current.pop() {
            break;
        }
    }
    None
}

/// Resolve an executable name against the system PATH.
fn resolve_executable(name: &str) -> Result<String, String> {
    if name.contains('\\') || name.contains('/') {
        return Ok(name.to_string());
    }

    let path_env = std::env::var("PATH").unwrap_or_default();
    for dir in path_env.split(';') {
        let base = std::path::Path::new(dir).join(name);
        if base.exists() {
            return Ok(base.to_string_lossy().to_string());
        }
        let with_exe = std::path::Path::new(dir).join(format!("{name}.exe"));
        if with_exe.exists() {
            return Ok(with_exe.to_string_lossy().to_string());
        }
    }

    Err(format!("找不到可执行文件: {name}"))
}

/// Path to the dedicated daemon log file.
fn daemon_log_path() -> Result<std::path::PathBuf, String> {
    Ok(crate::storage::log_dir()?.join("daemon.log"))
}

/// Ensure the parent directory of the daemon log exists.
fn ensure_daemon_log_dir() -> Result<(), String> {
    let path = daemon_log_path()?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|err| format!("无法创建日志目录 {}: {err}", parent.display()))?;
    }
    Ok(())
}

/// Append a line to the daemon log with timestamp and secret redaction.
fn append_daemon_log(line: &str) {
    let _ = ensure_daemon_log_dir();
    let Ok(path) = daemon_log_path() else {
        return;
    };

    let timestamp = chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f %:z");
    let safe_line = redact_secrets(line);
    let formatted = format!("[{}] {}\n", timestamp, safe_line);

    let mut file = match std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        Ok(f) => f,
        Err(_) => return,
    };
    let _ = file.write_all(formatted.as_bytes());
}

/// Create a fresh process snapshot to inspect processes by PID.
fn build_system() -> System {
    System::new_with_specifics(
        RefreshKind::nothing().with_processes(ProcessRefreshKind::everything()),
    )
}

/// Check whether a process with the given PID is still alive.
fn is_process_running(pid: u32) -> bool {
    let mut system = build_system();
    system.refresh_processes_specifics(
        sysinfo::ProcessesToUpdate::All,
        true,
        ProcessRefreshKind::everything(),
    );
    system.process(sysinfo::Pid::from_u32(pid)).is_some()
}

/// Start collecting stdout/stderr from the spawned child into the daemon log.
fn spawn_log_collectors(stdout: std::process::ChildStdout, stderr: std::process::ChildStderr) {
    thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines().flatten() {
            append_daemon_log(&format!("[stdout] {line}"));
        }
    });

    thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.lines().flatten() {
            append_daemon_log(&format!("[stderr] {line}"));
        }
    });
}

/// Resolve the executable and command arguments for launching the daemon.
/// Falls back from `naumi` to `python -m naumi_agent` only when no explicit executable was provided.
fn resolve_daemon_command(
    config: &DaemonLaunchConfig,
    port: u16,
) -> Result<(String, Vec<String>), String> {
    let requested = config
        .executable
        .clone()
        .unwrap_or_else(|| "naumi".to_string());
    let is_default = config.executable.is_none();

    if let Ok(resolved) = resolve_executable(&requested) {
        let (_, args) = build_daemon_command(config, port)?;
        return Ok((resolved, args));
    }

    // If the user explicitly requested an executable that is missing, do not silently fallback.
    if !is_default {
        return Err(format!("找不到可执行文件: {requested}"));
    }

    // Default fallback: use the Python module entry point.
    // Prefer the project's own virtual environment when available.
    let venv_python = find_venv_python().unwrap_or_else(|| {
        std::path::PathBuf::from(".venv")
            .join("Scripts")
            .join("python.exe")
    });
    let python_executable = if venv_python.exists() {
        venv_python.to_string_lossy().to_string()
    } else {
        resolve_executable("python")?
    };

    let python_config = DaemonLaunchConfig {
        executable: Some(python_executable),
        args: vec![
            "-m".to_string(),
            "naumi_agent".to_string(),
            "serve".to_string(),
            "--port".to_string(),
            port.to_string(),
        ],
        working_dir: config.working_dir.clone(),
        port: config.port,
        env_vars: config.env_vars.clone(),
    };
    let (_, args) = build_daemon_command(&python_config, port)?;
    Ok((python_config.executable.unwrap(), args))
}

async fn start_daemon_for_window(
    label: &str,
    config: DaemonLaunchConfig,
    persist: bool,
) -> Result<DaemonStatus, String> {
    // Restart only this window's daemon. Other windows keep running.
    let _ = stop_daemon_internal(label).await;
    let _start_guard = DAEMON_START_LOCK
        .lock()
        .map_err(|_| "守护进程启动锁被污染".to_string())?;

    let port = find_available_port(DEFAULT_PORT_RANGE)
        .ok_or_else(|| "端口范围 8765-8799 内无可用端口".to_string())?;

    let (resolved_executable, args) = resolve_daemon_command(&config, port)?;

    let mut cmd = Command::new(&resolved_executable);
    cmd.args(&args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    suppress_console_window(&mut cmd);

    if let Some(working_dir) = &config.working_dir {
        cmd.current_dir(working_dir);
    }

    for (key, value) in &config.env_vars {
        cmd.env(key, value);
    }

    let mut child = cmd
        .spawn()
        .map_err(|err| format!("无法启动守护进程 {resolved_executable}: {err}"))?;

    let pid = child.id();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    if let (Some(stdout), Some(stderr)) = (stdout, stderr) {
        spawn_log_collectors(stdout, stderr);
    }

    // Detach the child so the Rust runtime does not need to hold it.
    thread::spawn(move || {
        let _ = child.wait();
    });

    store_daemon_handle(
        label,
        DaemonHandle {
            pid,
            port,
            executable: resolved_executable.clone(),
        },
    )?;

    // Persist the effective launch configuration for later inspection.
    let persisted = DaemonLaunchConfig {
        executable: Some(resolved_executable.clone()),
        args: config.args.clone(),
        working_dir: config.working_dir.clone(),
        port: Some(port),
        env_vars: config.env_vars.clone(),
    };
    if persist {
        if let Ok(path) = daemon_launch_path() {
            let _ = write_json(&path, &persisted);
        }
    }

    let url = format!("http://127.0.0.1:{port}/api/v1");
    log_sync(
        "info",
        &format!("窗口 {label} 的守护进程已启动: {resolved_executable} PID={pid} 端口={port}"),
    );

    Ok(DaemonStatus {
        running: true,
        pid: Some(pid),
        port: Some(port),
        url: Some(url),
        executable: Some(resolved_executable),
        last_error: None,
    })
}

/// Launch the local NaumiAgent daemon for the invoking window.
#[command]
pub async fn start_daemon(
    window: WebviewWindow,
    config: DaemonLaunchConfig,
) -> Result<DaemonStatus, String> {
    start_daemon_for_window(window.label(), config, window.label() == "main").await
}

/// Internal helper to stop the tracked daemon.
pub(crate) fn stop_daemon_now(label: &str) -> Result<(), String> {
    let handle = take_daemon_handle(label)?;

    if let Some(handle) = handle {
        // Use taskkill /T /F to terminate the whole process tree on Windows.
        let mut cmd = Command::new("taskkill");
        cmd.args(["/T", "/F", "/PID", &handle.pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        suppress_console_window(&mut cmd);
        let _ = cmd.output();

        for _ in 0..PROCESS_TERMINATION_RETRIES {
            if !is_process_running(handle.pid) {
                break;
            }
            thread::sleep(Duration::from_millis(PROCESS_TERMINATION_DELAY_MS));
        }
    }

    log_sync("info", &format!("窗口 {label} 的守护进程已停止"));
    Ok(())
}

pub(crate) async fn stop_daemon_internal(label: &str) -> Result<(), String> {
    stop_daemon_now(label)
}

/// Stop the tracked daemon and clear its handle.
#[command]
pub async fn stop_daemon(window: WebviewWindow) -> Result<DaemonStatus, String> {
    stop_daemon_internal(window.label()).await?;
    Ok(DaemonStatus {
        running: false,
        pid: None,
        port: None,
        url: None,
        executable: None,
        last_error: None,
    })
}

/// Return the current daemon status, checking whether the process is alive.
#[command]
pub async fn get_daemon_status(window: WebviewWindow) -> Result<DaemonStatus, String> {
    let handle = daemon_handle(window.label())?;

    if let Some(handle) = handle {
        let running = is_process_running(handle.pid);
        return Ok(DaemonStatus {
            running,
            pid: Some(handle.pid),
            port: Some(handle.port),
            url: Some(format!("http://127.0.0.1:{}/api/v1", handle.port)),
            executable: Some(handle.executable),
            last_error: None,
        });
    }

    // Only the primary window falls back to the persisted launch config.
    if window.label() == "main" {
        if let Ok(Some(config)) = read_json::<DaemonLaunchConfig>(&daemon_launch_path()?) {
            return Ok(DaemonStatus {
                running: false,
                pid: None,
                port: config.port,
                url: config.port.map(|p| format!("http://127.0.0.1:{p}/api/v1")),
                executable: config.executable,
                last_error: None,
            });
        }
    }

    Ok(DaemonStatus {
        running: false,
        pid: None,
        port: None,
        url: None,
        executable: None,
        last_error: None,
    })
}

#[command]
pub async fn open_workspace_window(
    app: AppHandle,
    config: DaemonLaunchConfig,
    options: OpenWorkspaceWindowOptions,
) -> Result<OpenWorkspaceWindowResult, String> {
    let working_dir = config
        .working_dir
        .clone()
        .ok_or_else(|| "新窗口缺少工作目录".to_string())?;
    let path = PathBuf::from(&working_dir);
    if !path.is_dir() {
        return Err(format!("工作目录不存在或不是文件夹: {working_dir}"));
    }

    let label = workspace_window_label();
    let daemon = start_daemon_for_window(&label, config, false).await?;
    let api = daemon
        .url
        .as_deref()
        .ok_or_else(|| "新窗口的本地服务未返回地址".to_string())?;

    if let Some(port) = daemon.port {
        let mut ready = false;
        for _ in 0..200 {
            if is_port_reachable(port) {
                ready = true;
                break;
            }
            if !daemon_handle(&label)?.is_some_and(|handle| is_process_running(handle.pid)) {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
        if !ready {
            let _ = stop_daemon_internal(&label).await;
            return Err("新工作窗口的本地服务启动超时，请查看 daemon 日志".to_string());
        }
    }

    let window_url = workspace_window_url(
        &label,
        api,
        options.view.as_deref(),
        options.session_id.as_deref(),
    );

    let title = path
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .map(|name| format!("{name} - NaumiAgent"))
        .unwrap_or_else(|| "NaumiAgent".to_string());
    let built = WebviewWindowBuilder::new(&app, &label, WebviewUrl::App(window_url))
        .title(title)
        .inner_size(1440.0, 1024.0)
        .min_inner_size(980.0, 680.0)
        .resizable(true)
        .build();

    if let Err(error) = built {
        let _ = stop_daemon_internal(&label).await;
        return Err(format!("无法打开新的工作窗口: {error}"));
    }

    Ok(OpenWorkspaceWindowResult { label, daemon })
}

/// Read the most recent lines from the daemon log.
#[command]
pub async fn get_daemon_logs(limit: usize) -> Result<Vec<String>, String> {
    let path = daemon_log_path()?;
    if !path.exists() {
        return Ok(Vec::new());
    }

    let content = std::fs::read_to_string(&path)
        .map_err(|err| format!("无法读取日志文件 {}: {err}", path.display()))?;
    let lines: Vec<String> = content.lines().map(String::from).collect();
    let start = lines.len().saturating_sub(limit);
    Ok(lines[start..].to_vec())
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    #[test]
    fn find_available_port_returns_some() {
        // The returned port should not be reachable by us unless something is
        // actually bound to it.
        let port = find_available_port(DEFAULT_PORT_RANGE).expect("should find a port");
        assert!((DEFAULT_PORT_RANGE.0..=DEFAULT_PORT_RANGE.1).contains(&port));
    }

    #[test]
    fn build_daemon_command_uses_python_module() {
        let config = DaemonLaunchConfig {
            executable: Some("python".to_string()),
            args: Vec::new(),
            working_dir: None,
            port: None,
            env_vars: HashMap::new(),
        };
        let (exe, args) = build_daemon_command(&config, 8765).unwrap();
        assert_eq!(exe, "python");
        assert!(args.contains(&"-m".to_string()));
        assert!(args.contains(&"naumi_agent".to_string()));
        assert!(args.contains(&"8765".to_string()));
    }

    #[test]
    fn build_daemon_command_defaults_to_naumi_serve() {
        let config = DaemonLaunchConfig {
            executable: None,
            args: Vec::new(),
            working_dir: None,
            port: None,
            env_vars: HashMap::new(),
        };
        let (exe, args) = build_daemon_command(&config, 8766).unwrap();
        assert_eq!(exe, "naumi");
        assert!(args.contains(&"serve".to_string()));
        assert!(args.contains(&"8766".to_string()));
    }

    #[test]
    fn build_daemon_command_expands_port_placeholder() {
        let config = DaemonLaunchConfig {
            executable: Some("naumi".to_string()),
            args: vec![
                "serve".to_string(),
                "--port".to_string(),
                "{port}".to_string(),
            ],
            working_dir: None,
            port: None,
            env_vars: HashMap::new(),
        };
        let (_, args) = build_daemon_command(&config, 8777).unwrap();
        assert!(args.contains(&"8777".to_string()));
    }

    #[test]
    fn resolve_executable_fails_for_missing_command() {
        assert!(resolve_executable("definitely_not_a_real_binary_12345").is_err());
    }

    #[test]
    fn daemon_handles_are_isolated_by_window_label() {
        let first = "test-window-one";
        let second = "test-window-two";
        let _ = take_daemon_handle(first);
        let _ = take_daemon_handle(second);
        store_daemon_handle(
            first,
            DaemonHandle {
                pid: 11,
                port: 8765,
                executable: "naumi-one".to_string(),
            },
        )
        .unwrap();
        store_daemon_handle(
            second,
            DaemonHandle {
                pid: 22,
                port: 8766,
                executable: "naumi-two".to_string(),
            },
        )
        .unwrap();

        assert_eq!(take_daemon_handle(first).unwrap().unwrap().pid, 11);
        assert_eq!(daemon_handle(second).unwrap().unwrap().pid, 22);
        assert!(daemon_handle(first).unwrap().is_none());
        let _ = take_daemon_handle(second);
    }

    #[test]
    fn workspace_window_query_values_are_percent_encoded() {
        assert_eq!(
            encode_query_component("http://127.0.0.1:8765/api/v1"),
            "http%3A%2F%2F127.0.0.1%3A8765%2Fapi%2Fv1"
        );
        assert_eq!(encode_query_component("会话 1"), "%E4%BC%9A%E8%AF%9D%201");
        assert_eq!(
            workspace_window_url(
                "workspace-1",
                "http://127.0.0.1:8765/api/v1",
                Some("web2"),
                Some("会话 1"),
            )
            .to_string_lossy(),
            "index.html?naumiWindow=workspace-1&naumiApi=http%3A%2F%2F127.0.0.1%3A8765%2Fapi%2Fv1&naumiView=web2&naumiSession=%E4%BC%9A%E8%AF%9D%201"
        );
    }

    #[tokio::test]
    #[ignore = "requires a real naumi/python runtime on PATH; run with --ignored"]
    async fn parallel_daemon_lifecycle() {
        let label = "ignored-lifecycle-test-one";
        let second_label = "ignored-lifecycle-test-two";
        // Clean up any leftover daemon from previous runs.
        let _ = stop_daemon_internal(label).await;
        let _ = stop_daemon_internal(second_label).await;

        let workspace_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(4)
            .expect("workspace root")
            .to_path_buf();
        let mut env_vars = HashMap::new();
        env_vars.insert(
            "PYTHONPATH".to_string(),
            workspace_root.join("src").to_string_lossy().to_string(),
        );
        let config = DaemonLaunchConfig {
            executable: std::env::var("NAUMI_TEST_PYTHON").ok(),
            args: Vec::new(),
            working_dir: Some(workspace_root.to_string_lossy().to_string()),
            port: None,
            env_vars,
        };

        let status = start_daemon_for_window(label, config.clone(), false)
            .await
            .expect("should start daemon");
        let second = start_daemon_for_window(second_label, config, false)
            .await
            .expect("should start second daemon");
        assert!(status.running);
        assert!(status.pid.is_some());
        assert!(status.port.is_some());
        assert!(second.running);
        assert_ne!(status.port, second.port);
        assert!((DEFAULT_PORT_RANGE.0..=DEFAULT_PORT_RANGE.1).contains(&status.port.unwrap()));

        for _ in 0..100 {
            if status.port.is_some_and(is_port_reachable)
                && second.port.is_some_and(is_port_reachable)
            {
                break;
            }
            tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
        }
        assert!(status.port.is_some_and(is_port_reachable));
        assert!(second.port.is_some_and(is_port_reachable));

        let handle = daemon_handle(label)
            .expect("should get status")
            .expect("daemon handle");
        assert_eq!(handle.pid, status.pid.unwrap());
        eprintln!("daemon handle after sleep: {handle:?}");

        let logs = get_daemon_logs(50).await.expect("should read logs");
        eprintln!("daemon logs: {logs:?}");
        assert!(!logs.is_empty(), "daemon should have written startup logs");

        stop_daemon_internal(label)
            .await
            .expect("should stop daemon");
        assert!(daemon_handle(label)
            .expect("should get status after stop")
            .is_none());
        assert!(second.pid.is_some_and(is_process_running));
        stop_daemon_internal(second_label)
            .await
            .expect("should stop second daemon");
        assert!(daemon_handle(second_label)
            .expect("should get second status after stop")
            .is_none());
    }
}
