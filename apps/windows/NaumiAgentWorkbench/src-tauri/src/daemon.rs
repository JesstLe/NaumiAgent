use std::io::{BufRead, BufReader, Write};
use std::net::{SocketAddr, TcpStream};
use std::process::{Command, Stdio};
use std::sync::{LazyLock, Mutex};
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use sysinfo::{ProcessRefreshKind, RefreshKind, System};
use tauri::command;

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
}

static DAEMON_HANDLE: LazyLock<Mutex<Option<DaemonHandle>>> =
    LazyLock::new(|| Mutex::new(None));

/// Probe whether a local TCP port is currently reachable (occupied).
fn is_port_reachable(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let timeout = Duration::from_millis(PORT_PROBE_TIMEOUT_MS);
    TcpStream::connect_timeout(&addr, timeout).is_ok()
}

/// Find the first available port in the configured range.
fn find_available_port(range: (u16, u16)) -> Option<u16> {
    for port in range.0..=range.1 {
        if !is_port_reachable(port) {
            return Some(port);
        }
    }
    None
}

/// Build the executable and argument list for launching the daemon.
fn build_daemon_command(config: &DaemonLaunchConfig, port: u16) -> Result<(String, Vec<String>), String> {
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
            vec![
                "serve".to_string(),
                "--port".to_string(),
                port.to_string(),
            ]
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
fn resolve_daemon_command(config: &DaemonLaunchConfig, port: u16) -> Result<(String, Vec<String>), String> {
    let requested = config.executable.clone().unwrap_or_else(|| "naumi".to_string());
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
    let venv_python = find_venv_python().unwrap_or_else(|| std::path::PathBuf::from(".venv").join("Scripts").join("python.exe"));
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

/// Launch the local NaumiAgent daemon on an available port.
#[command]
pub async fn start_daemon(config: DaemonLaunchConfig) -> Result<DaemonStatus, String> {
    // Ensure no existing daemon is still tracked.
    let _ = stop_daemon_internal().await;

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

    let mut child = cmd.spawn().map_err(|err| {
        format!(
            "无法启动守护进程 {resolved_executable}: {err}"
        )
    })?;

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

    {
        let mut handle = DAEMON_HANDLE
            .lock()
            .map_err(|_| "守护进程状态锁被污染".to_string())?;
        *handle = Some(DaemonHandle { pid, port });
    }

    // Persist the effective launch configuration for later inspection.
    let persisted = DaemonLaunchConfig {
        executable: Some(resolved_executable.clone()),
        args: args.clone(),
        working_dir: config.working_dir.clone(),
        port: Some(port),
        env_vars: config.env_vars.clone(),
    };
    if let Ok(path) = daemon_launch_path() {
        let _ = write_json(&path, &persisted);
    }

    let url = format!("http://127.0.0.1:{port}/api/v1");
    log_sync(
        "info",
        &format!("守护进程已启动: {resolved_executable} PID={pid} 端口={port}"),
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

/// Internal helper to stop the tracked daemon.
async fn stop_daemon_internal() -> Result<(), String> {
    let handle = {
        let mut guard = DAEMON_HANDLE
            .lock()
            .map_err(|_| "守护进程状态锁被污染".to_string())?;
        guard.take()
    };

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

    log_sync("info", "守护进程已停止");
    Ok(())
}

/// Stop the tracked daemon and clear its handle.
#[command]
pub async fn stop_daemon() -> Result<DaemonStatus, String> {
    stop_daemon_internal().await?;
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
pub async fn get_daemon_status() -> Result<DaemonStatus, String> {
    let handle = DAEMON_HANDLE
        .lock()
        .map_err(|_| "守护进程状态锁被污染".to_string())?
        .clone();

    if let Some(handle) = handle {
        let running = is_process_running(handle.pid);
        return Ok(DaemonStatus {
            running,
            pid: Some(handle.pid),
            port: Some(handle.port),
            url: Some(format!("http://127.0.0.1:{}/api/v1", handle.port)),
            executable: None,
            last_error: None,
        });
    }

    // No in-memory handle; check whether a launch config was persisted.
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

    Ok(DaemonStatus {
        running: false,
        pid: None,
        port: None,
        url: None,
        executable: None,
        last_error: None,
    })
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
            args: vec!["serve".to_string(), "--port".to_string(), "{port}".to_string()],
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

    #[tokio::test]
    #[ignore = "requires a real naumi/python runtime on PATH; run with --ignored"]
    async fn start_and_stop_daemon_lifecycle() {
        // Clean up any leftover daemon from previous runs.
        let _ = stop_daemon_internal().await;

        let config = DaemonLaunchConfig {
            executable: None,
            args: Vec::new(),
            working_dir: None,
            port: None,
            env_vars: HashMap::new(),
        };

        let status = start_daemon(config).await.expect("should start daemon");
        assert!(status.running);
        assert!(status.pid.is_some());
        assert!(status.port.is_some());
        assert!((DEFAULT_PORT_RANGE.0..=DEFAULT_PORT_RANGE.1).contains(&status.port.unwrap()));

        // Give the Python process a moment to initialize.
        tokio::time::sleep(tokio::time::Duration::from_secs(3)).await;

        let status = get_daemon_status().await.expect("should get status");
        assert!(status.pid.is_some());
        eprintln!("daemon status after sleep: {status:?}");

        let logs = get_daemon_logs(50).await.expect("should read logs");
        eprintln!("daemon logs: {logs:?}");
        assert!(!logs.is_empty(), "daemon should have written startup logs");

        let stopped = stop_daemon().await.expect("should stop daemon");
        assert!(!stopped.running);

        let status = get_daemon_status().await.expect("should get status after stop");
        assert!(!status.running);
    }
}
