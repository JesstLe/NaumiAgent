use std::fs;
use std::io::Write;

use chrono::{Datelike, Local};
use regex::Regex;
use tauri::command;

use crate::storage::{ensure_app_dirs, log_dir};

const LOG_RETENTION_DAYS: i64 = 7;
const LOG_FILE_PREFIX: &str = "app";

/// Returns the path for today's application log file.
pub fn current_log_path() -> Result<std::path::PathBuf, String> {
    let today = Local::now();
    let filename = format!("{}-{:04}-{:02}-{:02}.log", LOG_FILE_PREFIX, today.year(), today.month(), today.day());
    Ok(log_dir()?.join(filename))
}

/// Removes application log files older than `LOG_RETENTION_DAYS`.
pub fn rotate_logs() -> Result<(), String> {
    let dir = log_dir()?;
    if !dir.exists() {
        return Ok(());
    }

    let cutoff = Local::now() - chrono::Duration::days(LOG_RETENTION_DAYS);
    let entries = fs::read_dir(&dir)
        .map_err(|err| format!("无法读取日志目录 {}: {err}", dir.display()))?;

    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_file() {
            if let Ok(metadata) = entry.metadata() {
                if let Ok(modified) = metadata.modified() {
                    let modified_time = chrono::DateTime::<Local>::from(modified);
                    if modified_time < cutoff {
                        let _ = fs::remove_file(&path);
                    }
                }
            }
        }
    }
    Ok(())
}

/// Redacts secrets from a log message.
pub fn redact_secrets(message: &str) -> String {
    // Bearer tokens after "Bearer " or "bearer ".
    let bearer_re = Regex::new(r#"(?i)(Bearer\s+)\S+"#).unwrap();
    // API keys in query strings, JSON values, or form fields.
    let api_key_re = Regex::new(r#"(?i)(\"?api[_-]?key\"?)(\s*[=:]\s*[\"']?)([^\s&\"',}\]\n]+)"#).unwrap();
    // Generic authorization headers.
    let auth_re = Regex::new(r#"(?i)(Authorization)(\s*[=:]\s*[\"']?)([^\s&\"',}\]\n]+)"#).unwrap();

    let mut redacted = bearer_re.replace_all(message, "${1}<redacted>").to_string();
    redacted = api_key_re.replace_all(&redacted, "${1}${2}<redacted>").to_string();
    redacted = auth_re.replace_all(&redacted, "${1}${2}<redacted>").to_string();
    redacted
}

/// Writes a log line to today's log file with the local timestamp.
#[command]
pub fn write_app_log(level: String, message: String) -> Result<(), String> {
    ensure_app_dirs()?;
    rotate_logs()?;

    let path = current_log_path()?;
    let timestamp = Local::now().format("%Y-%m-%d %H:%M:%S%.3f %:z");
    let safe_message = redact_secrets(&message);
    let line = format!("[{}] [{}] {}\n", timestamp, level.to_uppercase(), safe_message);

    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|err| format!("无法打开日志文件 {}: {err}", path.display()))?;

    file.write_all(line.as_bytes())
        .map_err(|err| format!("无法写入日志文件 {}: {err}", path.display()))?;

    Ok(())
}

/// Helper used by Rust code to log synchronously without invoking Tauri.
#[allow(dead_code)]
pub fn log_sync(level: &str, message: &str) {
    let _ = ensure_app_dirs();
    let _ = rotate_logs();
    if let Ok(path) = current_log_path() {
        let timestamp = Local::now().format("%Y-%m-%d %H:%M:%S%.3f %:z");
        let safe_message = redact_secrets(message);
        let line = format!("[{}] [{}] {}\n", timestamp, level.to_uppercase(), safe_message);
        let mut file = match fs::OpenOptions::new().create(true).append(true).open(&path) {
            Ok(f) => f,
            Err(_) => return,
        };
        let _ = file.write_all(line.as_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::env;

    #[test]
    fn current_log_path_contains_date() {
        let path = current_log_path().unwrap();
        let filename = path.file_name().unwrap().to_string_lossy();
        assert!(filename.starts_with(LOG_FILE_PREFIX));
        assert!(filename.ends_with(".log"));
    }

    #[test]
    fn redact_bearer_token() {
        let input = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9";
        let output = redact_secrets(input);
        assert!(output.contains("<redacted>"));
        assert!(!output.contains("eyJhbGciOi"));
    }

    #[test]
    fn redact_api_key_query_param() {
        let input = "ws://localhost:8765/ws?api_key=super_secret_token_123";
        let output = redact_secrets(input);
        assert!(output.contains("<redacted>"));
        assert!(!output.contains("super_secret_token_123"));
    }

    #[test]
    fn redact_json_api_key() {
        let input = r#"{"api_key": "abc123", "name": "test"}"#;
        let output = redact_secrets(input);
        assert!(output.contains("<redacted>"));
        assert!(!output.contains("abc123"));
    }

    #[test]
    fn log_line_has_expected_format() {
        let temp_dir = env::temp_dir().join(format!("naumi-log-test-{}", std::process::id()));
        fs::create_dir_all(&temp_dir).unwrap();
        let log_file = temp_dir.join("test.log");

        let line = format!(
            "[{}] [INFO] hello world\n",
            Local::now().format("%Y-%m-%d %H:%M:%S%.3f %:z")
        );
        fs::write(&log_file, &line).unwrap();

        let content = fs::read_to_string(&log_file).unwrap();
        assert!(content.contains("INFO"));
        assert!(content.contains("hello world"));
    }
}
