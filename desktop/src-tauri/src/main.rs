#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::OpenOptions;
use std::io::Write;
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, RunEvent, WindowEvent};

const APP_URL: &str = "http://127.0.0.1:8721";
const APP_PORT: u16 = 8721;

#[derive(Default)]
struct Server(Mutex<Option<Child>>);

fn health() -> bool {
    TcpStream::connect_timeout(
        &SocketAddr::from(([127, 0, 0, 1], APP_PORT)),
        Duration::from_millis(400),
    )
    .is_ok()
}

fn repo_root() -> Option<PathBuf> {
    if let Ok(v) = std::env::var("WECHAT_HOME") {
        let p = PathBuf::from(v);
        if p.join("backend").join("server.py").is_file() {
            return Some(p);
        }
    }
    let exe = std::env::current_exe().ok()?;
    let mut dir = exe.parent()?.to_path_buf();
    for _ in 0..6 {
        if dir.join("backend").join("server.py").is_file() {
            return Some(dir);
        }
        dir = dir.parent()?.to_path_buf();
    }
    None
}

fn spawn_server(root: &Path) -> Option<Child> {
    if health() {
        log_line("server already running, attach");
        return None;
    }
    log_line("spawning python server");
    let backend = root.join("backend");
    let log = root.join("data").join("desktop.log");
    let mut log_file = OpenOptions::new().create(true).append(true).open(&log).ok();
    for exe in ["pythonw.exe", "python.exe"] {
        let mut cmd = Command::new(exe);
        cmd.arg("server.py").arg(APP_PORT.to_string()).current_dir(&backend);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }
        if let Some(f) = log_file.as_mut() {
            match f.try_clone() {
                Ok(f2) => {
                    cmd.stdout(Stdio::from(f2));
                    match f.try_clone() {
                        Ok(f3) => {
                            cmd.stderr(Stdio::from(f3));
                        }
                        Err(_) => {
                            cmd.stderr(Stdio::null());
                        }
                    }
                }
                Err(_) => {
                    cmd.stdout(Stdio::null()).stderr(Stdio::null());
                }
            }
        } else {
            cmd.stdout(Stdio::null()).stderr(Stdio::null());
        }
        if let Ok(child) = cmd.spawn() {
            return Some(child);
        }
    }
    None
}

fn wait_ready(timeout: Duration) -> bool {
    let started = Instant::now();
    while started.elapsed() < timeout {
        if health() {
            return true;
        }
        thread::sleep(Duration::from_millis(400));
    }
    false
}

fn log_line(msg: &str) {
    if let Some(root) = repo_root() {
        if let Ok(mut f) = OpenOptions::new().create(true).append(true)
            .open(root.join("data").join("desktop.log"))
        {
            let secs = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let _ = writeln!(f, "[{}] {}", secs, msg);
        }
    }
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
        log_line("show main window");
    }
}

fn enter_app(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        // 初始页是本地启动页，服务就绪后再跳到真正的界面。
        let _ = w.eval(format!("window.location.replace('{}')", APP_URL));
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
        log_line("service ready, enter app");
    }
}

fn set_boot_state(app: &tauri::AppHandle, msg: &str) {
    if let Some(w) = app.get_webview_window("main") {
        let js = format!(
            "const el=document.getElementById('bootState'); if(el) el.textContent='{}';",
            msg.replace('\\', "\\\\").replace('\'', "\\'")
        );
        let _ = w.eval(js);
    }
}

fn stop_server(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Server>() {
        if let Some(mut child) = state.0.lock().ok().and_then(|mut g| g.take()) {
            log_line("stopping python server");
            #[cfg(windows)]
            {
                let _ = Command::new("taskkill")
                    .args(["/PID", &child.id().to_string(), "/T", "/F"])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn build_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "显示主界面", true, None::<&str>)?;
    let browse = MenuItem::with_id(app, "browse", "在浏览器打开", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &browse, &quit])?;
    let icon = app.default_window_icon().cloned().expect("缺少应用图标");
    TrayIconBuilder::with_id("main-tray")
        .icon(icon)
        .tooltip("Qwent")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main(app),
            "browse" => {
                let _ = Command::new("explorer.exe").arg(APP_URL).spawn();
            }
            "quit" => {
                stop_server(app);
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main(tray.app_handle());
            }
        })
        .build(app)?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main(app);
        }))
        .manage(Server::default())
        .setup(|app| {
            let handle = app.handle().clone();
            build_tray(&handle)?;
            let root = repo_root();
            let boot = handle.clone();
            thread::spawn(move || {
                let already = health();
                if already {
                    log_line("server already running, attach");
                } else {
                    set_boot_state(&boot, "正在启动本机服务（首次冷启动可能要十几秒）…");
                    let child = root.as_deref().and_then(spawn_server);
                    if let Some(c) = child {
                        if let Some(state) = boot.try_state::<Server>() {
                            if let Ok(mut g) = state.0.lock() {
                                *g = Some(c);
                            }
                        }
                    }
                }
                if wait_ready(Duration::from_secs(60)) {
                    enter_app(&boot);
                } else {
                    // 服务没起来：留在本地启动页，把排查步骤写到状态行。
                    set_boot_state(&boot,
                        "服务未能启动：请先双击一键配置.bat，或运行启动助手.bat 查看报错。");
                    show_main(&boot);
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("启动桌面壳失败")
        .run(|app, event| {
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                stop_server(app);
            }
        });
}
