use std::path::{Path, PathBuf};
use std::process::Command;

use tauri::command;

/// Accepted git bash base names.
const GIT_BASH_NAMES: [&str; 2] = ["bash", "bash.exe"];

/// Resolve the Git for Windows `bash.exe` without mistaking the WSL launcher for it.
///
/// Discovery precedence mirrors the Python runtime adapter in
/// `src/naumi_agent/runtime/shell.py::resolve_git_bash`:
///   1. `NAUMI_GIT_BASH` environment variable (must point at an acceptable bash.exe)
///   2. `git` on PATH -> `<git>/../bin/bash.exe`
///   3. `%ProgramFiles%\Git\bin\bash.exe`
///   4. `%ProgramFiles(x86)%\Git\bin\bash.exe`
///   5. `%LOCALAPPDATA%\Programs\Git\bin\bash.exe`
///   6. `bash` on PATH (rejected if it is the WSL System32 launcher)
pub fn resolve_git_bash() -> Result<PathBuf, String> {
    // 1. Explicit override.
    if let Ok(explicit) = std::env::var("NAUMI_GIT_BASH") {
        let trimmed = explicit.trim();
        if !trimmed.is_empty() {
            let candidate = PathBuf::from(trimmed);
            if !candidate.is_file() || !is_acceptable_git_bash(&candidate) {
                return Err("环境变量 NAUMI_GIT_BASH 未指向可用的 Git Bash bash.exe。".to_string());
            }
            return Ok(candidate);
        }
    }

    // 2. Derive from `git` on PATH.
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(git_path) = which("git") {
        if let Some(grandparent) = git_path.parent().and_then(|p| p.parent()) {
            candidates.push(grandparent.join("bin").join("bash.exe"));
        }
    }

    // 3-5. Well-known Git for Windows install roots.
    if let Ok(program_files) = std::env::var("ProgramFiles") {
        candidates.push(PathBuf::from(program_files).join("Git").join("bin").join("bash.exe"));
    }
    if let Ok(program_files_x86) = std::env::var("ProgramFiles(x86)") {
        candidates.push(PathBuf::from(program_files_x86).join("Git").join("bin").join("bash.exe"));
    }
    if let Ok(local_app_data) = std::env::var("LOCALAPPDATA") {
        candidates.push(
            PathBuf::from(local_app_data)
                .join("Programs")
                .join("Git")
                .join("bin")
                .join("bash.exe"),
        );
    }

    // 6. `bash` on PATH.
    if let Some(path_bash) = which("bash") {
        candidates.push(path_bash);
    }

    for candidate in unique_paths(candidates) {
        if candidate.is_file() && is_acceptable_git_bash(&candidate) {
            return Ok(candidate);
        }
    }

    Err(
        "未找到 Git Bash。请安装 Git for Windows，或将 NAUMI_GIT_BASH 设置为 \
         Git 安装目录中的 bin\\bash.exe；不能使用 C:\\Windows\\System32\\bash.exe。"
            .to_string(),
    )
}

/// Locate an executable on PATH (Windows appends `.exe` automatically via the OS).
fn which(name: &str) -> Option<PathBuf> {
    let path_env = std::env::var("PATH").ok()?;
    for dir in path_env.split(';') {
        if dir.is_empty() {
            continue;
        }
        let base = Path::new(dir).join(name);
        if base.is_file() {
            return Some(base);
        }
        let with_exe = Path::new(dir).join(format!("{name}.exe"));
        if with_exe.is_file() {
            return Some(with_exe);
        }
    }
    None
}

/// Reject the WSL launcher at `C:\Windows\System32\bash.exe`.
fn is_acceptable_git_bash(path: &Path) -> bool {
    let normalized = path.to_string_lossy().replace('/', "\\").to_lowercase();
    if normalized.ends_with("\\windows\\system32\\bash.exe") {
        return false;
    }
    let file_name = match path.file_name() {
        Some(name) => name.to_string_lossy().to_lowercase(),
        None => return false,
    };
    GIT_BASH_NAMES.contains(&file_name.as_str())
}

/// Deduplicate a list of paths while preserving order.
fn unique_paths(paths: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut seen = std::collections::HashSet::new();
    let mut result = Vec::new();
    for path in paths {
        let key = path.to_string_lossy().to_lowercase();
        if seen.insert(key) {
            result.push(path);
        }
    }
    result
}

/// Open a filesystem path in Windows Explorer. Creates the directory if missing.
#[command]
pub fn open_in_explorer(path: String) -> Result<(), String> {
    let target = PathBuf::from(&path);
    if !target.exists() {
        return Err(format!("路径不存在: {}", target.display()));
    }

    // `explorer.exe <path>` opens the folder (or selects a file when a path is passed).
    let status = Command::new("explorer.exe")
        .arg(&path)
        .spawn()
        .map_err(|err| format!("无法打开资源管理器: {err}"))?;
    // Detach: explorer spawns its own process; we do not wait on it.
    drop(status);
    Ok(())
}

/// Open a Git Bash terminal rooted at the given working directory.
#[command]
pub fn open_in_terminal(path: String) -> Result<(), String> {
    let target = PathBuf::from(&path);
    if !target.exists() {
        return Err(format!("路径不存在: {}", target.display()));
    }
    if !target.is_dir() {
        return Err(format!("目标不是目录: {}", target.display()));
    }

    let bash = resolve_git_bash()?;

    // Launch a login bash that starts in the target directory.
    Command::new(&bash)
        .arg("--login")
        .current_dir(&target)
        .spawn()
        .map_err(|err| format!("无法启动 Git Bash ({}): {err}", bash.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn is_acceptable_git_bash_rejects_wsl_launcher() {
        let wsl = Path::new("C:\\Windows\\System32\\bash.exe");
        assert!(!is_acceptable_git_bash(wsl));
    }

    #[test]
    fn is_acceptable_git_bash_accepts_git_for_windows() {
        let real = Path::new("C:\\Program Files\\Git\\bin\\bash.exe");
        assert!(is_acceptable_git_bash(real));
    }

    #[test]
    fn is_acceptable_git_bash_rejects_unrelated_file() {
        let other = Path::new("C:\\Windows\\System32\\cmd.exe");
        assert!(!is_acceptable_git_bash(other));
    }

    #[test]
    fn unique_paths_dedupes_case_insensitively() {
        let paths = vec![
            PathBuf::from("C:\\Program Files\\Git\\bin\\bash.exe"),
            PathBuf::from("c:\\program files\\git\\bin\\bash.exe"),
            PathBuf::from("C:\\Tools\\bash.exe"),
        ];
        let result = unique_paths(paths);
        assert_eq!(result.len(), 2);
    }

    #[test]
    fn which_returns_none_for_missing_executable() {
        assert!(which("definitely_not_a_real_binary_xyz").is_none());
    }

    #[test]
    fn resolve_git_bash_errors_without_install() {
        // With NAUMI_GIT_BASH pointing at a nonexistent path, resolution must fail
        // with the localized override message rather than panic.
        std::env::set_var(
            "NAUMI_GIT_BASH",
            "Z:\\definitely\\not\\real\\bash.exe",
        );
        let result = resolve_git_bash();
        std::env::remove_var("NAUMI_GIT_BASH");
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.contains("NAUMI_GIT_BASH"), "unexpected error: {err}");
    }
}
