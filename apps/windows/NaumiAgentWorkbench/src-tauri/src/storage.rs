use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::command;

const APP_DIR_NAME: &str = "NaumiAgentWorkbench";
const SETTINGS_FILE: &str = "settings.json";
const DAEMON_LAUNCH_FILE: &str = "daemon-launch.json";
#[allow(dead_code)]
const WORKSPACE_REGISTRY_FILE: &str = "workspace-registry.json";
const LOGS_DIR: &str = "logs";
const DATA_DIR: &str = "data";

/// Returns the root application data directory (`%LOCALAPPDATA%\\NaumiAgentWorkbench`).
pub fn app_data_dir() -> Result<PathBuf, String> {
    dirs::data_local_dir()
        .map(|dir| dir.join(APP_DIR_NAME))
        .ok_or_else(|| "无法定位本地应用数据目录".to_string())
}

/// Ensures that the application data, data, and logs directories exist.
pub fn ensure_app_dirs() -> Result<(), String> {
    let root = app_data_dir()?;
    for sub in [DATA_DIR, LOGS_DIR] {
        let path = root.join(sub);
        fs::create_dir_all(&path).map_err(|err| format!("无法创建目录 {}: {err}", path.display()))?;
    }
    Ok(())
}

/// Path to the user settings JSON file.
pub fn settings_path() -> Result<PathBuf, String> {
    Ok(app_data_dir()?.join(SETTINGS_FILE))
}

/// Path to the daemon launch configuration file.
pub fn daemon_launch_path() -> Result<PathBuf, String> {
    Ok(app_data_dir()?.join(DATA_DIR).join(DAEMON_LAUNCH_FILE))
}

/// Path to the workspace registry file.
#[allow(dead_code)]
pub fn workspace_registry_path() -> Result<PathBuf, String> {
    Ok(app_data_dir()?.join(DATA_DIR).join(WORKSPACE_REGISTRY_FILE))
}

/// Directory where application logs are written.
pub fn log_dir() -> Result<PathBuf, String> {
    Ok(app_data_dir()?.join(LOGS_DIR))
}

/// Persists a JSON-serializable value to a file, creating parent directories if needed.
pub fn write_json<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("无法创建目录 {}: {err}", parent.display()))?;
    }
    let content = serde_json::to_string_pretty(value)
        .map_err(|err| format!("序列化失败: {err}"))?;
    fs::write(path, content).map_err(|err| format!("无法写入 {}: {err}", path.display()))
}

/// Reads a JSON file and deserializes it; returns `None` if the file does not exist.
pub fn read_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<Option<T>, String> {
    match fs::read_to_string(path) {
        Ok(content) => {
            let value = serde_json::from_str(&content)
                .map_err(|err| format!("无法解析 {}: {err}", path.display()))?;
            Ok(Some(value))
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(err) => Err(format!("无法读取 {}: {err}", path.display())),
    }
}

/// Reads the entire settings object from disk.
fn read_settings() -> Result<serde_json::Map<String, serde_json::Value>, String> {
    let path = settings_path()?;
    match read_json::<serde_json::Value>(&path)? {
        Some(serde_json::Value::Object(map)) => Ok(map),
        Some(_) => Ok(serde_json::Map::new()),
        None => Ok(serde_json::Map::new()),
    }
}

/// Writes the settings object back to disk.
fn write_settings(map: serde_json::Map<String, serde_json::Value>) -> Result<(), String> {
    let path = settings_path()?;
    write_json(&path, &serde_json::Value::Object(map))
}

/// Configuration used to launch the local NaumiAgent daemon.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DaemonLaunchConfig {
    pub executable: Option<String>,
    pub args: Vec<String>,
    pub working_dir: Option<String>,
    pub port: Option<u16>,
    pub env_vars: std::collections::HashMap<String, String>,
}

#[command]
pub fn get_setting(key: String) -> Result<Option<String>, String> {
    let settings = read_settings()?;
    Ok(settings.get(&key).and_then(|value| value.as_str().map(String::from)))
}

#[command]
pub fn set_setting(key: String, value: String) -> Result<(), String> {
    let mut settings = read_settings()?;
    settings.insert(key, serde_json::Value::String(value));
    write_settings(settings)
}

#[command]
pub fn get_daemon_launch_config() -> Result<Option<DaemonLaunchConfig>, String> {
    let path = daemon_launch_path()?;
    read_json(&path)
}

#[command]
pub fn set_daemon_launch_config(config: DaemonLaunchConfig) -> Result<(), String> {
    let path = daemon_launch_path()?;
    write_json(&path, &config)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_data_dir_uses_local_app_data() {
        let dir = app_data_dir().expect("should resolve app data dir");
        assert!(dir.to_string_lossy().contains(APP_DIR_NAME));
    }

    #[test]
    fn settings_path_inside_app_data() {
        let root = app_data_dir().unwrap();
        assert_eq!(settings_path().unwrap(), root.join(SETTINGS_FILE));
    }

    #[test]
    fn daemon_launch_path_inside_data_subdir() {
        let root = app_data_dir().unwrap();
        assert_eq!(daemon_launch_path().unwrap(), root.join(DATA_DIR).join(DAEMON_LAUNCH_FILE));
    }

    #[test]
    fn log_dir_inside_app_data() {
        let root = app_data_dir().unwrap();
        assert_eq!(log_dir().unwrap(), root.join(LOGS_DIR));
    }

    #[test]
    fn read_missing_settings_returns_empty_map() {
        // Use a temporary file path by mocking via the public API is hard because
        // settings_path is fixed; instead we just verify the shape of an empty read.
        let map = read_settings().expect("empty settings should be readable");
        assert!(map.is_empty());
    }

    #[test]
    fn write_and_read_setting_roundtrip() {
        let temp_dir = std::env::temp_dir().join(format!(
            "naumi-storage-test-{}",
            std::process::id()
        ));
        fs::create_dir_all(&temp_dir).unwrap();
        let settings_file = temp_dir.join("settings.json");
        let map = serde_json::Map::from_iter([
            ("locale".to_string(), serde_json::Value::String("zh-CN".to_string())),
        ]);
        write_json(&settings_file, &serde_json::Value::Object(map)).unwrap();
        let restored: serde_json::Value = read_json(&settings_file).unwrap().unwrap();
        assert_eq!(restored["locale"], "zh-CN");
    }

    #[test]
    fn daemon_launch_config_serializes() {
        let config = DaemonLaunchConfig {
            executable: Some("python".to_string()),
            args: vec!["-m".to_string(), "naumi_agent".to_string(), "api".to_string()],
            working_dir: Some("C:\\projects".to_string()),
            port: Some(8765),
            env_vars: std::collections::HashMap::new(),
        };
        let json = serde_json::to_string(&config).unwrap();
        assert!(json.contains("naumi_agent"));
    }
}
