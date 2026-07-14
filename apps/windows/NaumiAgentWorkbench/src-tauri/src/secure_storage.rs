use keyring::Entry;
use tauri::command;

const SERVICE: &str = "com.naumi.workbench";
const USERNAME: &str = "api-key";

fn entry() -> keyring::Result<Entry> {
    Entry::new(SERVICE, USERNAME)
}

#[command]
pub fn get_token() -> Result<Option<String>, String> {
    match entry() {
        Ok(entry) => match entry.get_password() {
            Ok(token) if !token.is_empty() => Ok(Some(token)),
            Ok(_) => Ok(None),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(err) => Err(format!("无法读取安全令牌: {err}")),
        },
        Err(err) => Err(format!("无法初始化凭据存储: {err}")),
    }
}

#[command]
pub fn set_token(token: String) -> Result<(), String> {
    match entry() {
        Ok(entry) => entry
            .set_password(&token)
            .map_err(|err| format!("无法保存安全令牌: {err}")),
        Err(err) => Err(format!("无法初始化凭据存储: {err}")),
    }
}

#[command]
pub fn remove_token() -> Result<(), String> {
    match entry() {
        Ok(entry) => match entry.delete_credential() {
            Ok(()) => Ok(()),
            Err(keyring::Error::NoEntry) => Ok(()),
            Err(err) => Err(format!("无法删除安全令牌: {err}")),
        },
        Err(err) => Err(format!("无法初始化凭据存储: {err}")),
    }
}
