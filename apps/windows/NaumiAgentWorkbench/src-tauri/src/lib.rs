mod daemon;
mod logging;
mod secure_storage;
mod shell;
mod storage;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            secure_storage::get_token,
            secure_storage::set_token,
            secure_storage::remove_token,
            storage::get_setting,
            storage::set_setting,
            storage::get_daemon_launch_config,
            storage::set_daemon_launch_config,
            logging::write_app_log,
            daemon::start_daemon,
            daemon::stop_daemon,
            daemon::get_daemon_status,
            daemon::get_daemon_logs,
            shell::open_in_explorer,
            shell::open_in_terminal,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
