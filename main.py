import os
import time
import signal
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from datetime import datetime

# --- 配置项 ---
SERVER_URL = "https://panel.godlike.host/server/616fcc4a"
LOGIN_URL = "https://panel.godlike.host/auth/login"
COOKIE_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
TASK_TIMEOUT_SECONDS = 300  # 5分钟

# --- 超时处理机制 ---
class TaskTimeoutError(Exception):
    """自定义任务超时异常"""
    pass

def timeout_handler(signum, frame):
    """超时信号处理函数"""
    raise TaskTimeoutError("任务执行时间超过设定的阈值")

if os.name != 'nt':
    signal.signal(signal.SIGALRM, timeout_handler)


# ==================== 功能1：验证代理出口 IP ====================
def verify_proxy_ip(page):
    socks5_proxy = os.environ.get('SOCKS5_PROXY')
    if not socks5_proxy:
        print("未设置 SOCKS5_PROXY，跳过代理 IP 验证，使用默认出口。")
        return True

    print("已启用 SOCKS5 代理，正在验证出口 IP...")
    try:
        page.goto("https://api.ipify.org?format=text", wait_until="domcontentloaded", timeout=20000)
        current_ip = page.locator("body").inner_text().strip()
        print(f"✅ 当前出口 IP 验证成功: {current_ip}")
        return True
    except Exception as e:
        print(f"❌ 代理 IP 验证失败，无法访问 ipify: {e}", flush=True)
        page.screenshot(path="proxy_verify_error.png")
        return False
# ==============================================================


def login_with_playwright(page):
    """处理登录逻辑，优先使用Cookie，失败则使用邮箱密码。"""
    remember_web_cookie = os.environ.get('PTERODACTYL_COOKIE')
    pterodactyl_email = os.environ.get('PTERODACTYL_EMAIL')
    pterodactyl_password = os.environ.get('PTERODACTYL_PASSWORD')

    if remember_web_cookie:
        print("检测到 PTERODACTYL_COOKIE，尝试使用 Cookie 登录...")
        session_cookie = {
            'name': COOKIE_NAME, 'value': remember_web_cookie, 'domain': '.panel.godlike.host',
            'path': '/', 'expires': int(time.time()) + 3600 * 24 * 365, 'httpOnly': True,
            'secure': True, 'sameSite': 'Lax'
        }
        page.context.add_cookies([session_cookie])
        print(f"已设置 Cookie。正在访问目标服务器页面: {SERVER_URL}")
        page.goto(SERVER_URL, wait_until="domcontentloaded")

        if "auth/login" in page.url:
            print("Cookie 登录失败或会话已过期，将回退到邮箱密码登录。")
            page.context.clear_cookies()
        else:
            print("Cookie 登录成功！")
            return True

    if not (pterodactyl_email and pterodactyl_password):
        print("错误: Cookie 无效或未提供，且未提供 PTERODACTYL_EMAIL 和 PTERODACTYL_PASSWORD。无法登录。", flush=True)
        return False

    print("正在尝试使用邮箱和密码登录...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    try:
        print("正在点击 'Through login/password'...")
        page.locator('a:has-text("Through login/password")').click()

        email_selector = 'input[name="username"]'
        password_selector = 'input[name="password"]'
        login_button_selector = 'button[type="submit"]:has-text("Login")'

        print("等待登录表单元素加载...")
        page.wait_for_selector(email_selector)
        page.wait_for_selector(password_selector)
        print("正在填写邮箱和密码...")
        page.fill(email_selector, pterodactyl_email)
        page.fill(password_selector, pterodactyl_password)
        print("正在点击登录按钮...")
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.click(login_button_selector)

        if "auth/login" in page.url:
            print("邮箱密码登录失败，请检查凭据是否正确。", flush=True)
            page.screenshot(path="login_fail_error.png")
            return False

        print("邮箱密码登录成功！")
        return True
    except Exception as e:
        print(f"邮箱密码登录过程中发生错误: {e}", flush=True)
        page.screenshot(path="login_process_error.png")
        return False


# ==================== 功能2：检查并处理 offline 状态 ====================
def ensure_server_online(page):
    """
    续期前检查服务器状态。
    若服务器处于 Offline 状态，则点击 Start 按钮并等待其上线。
    Starting / Running 均视为正常状态。
    所有异常均降级处理，不中断续期流程。
    """
    print("正在检查服务器运行状态...")
    try:
        status_selector = '[class*="ServerConsole___StyledSpan4"]'
        page.wait_for_selector(status_selector, timeout=15000)

        print("等待 WebSocket 连接完成...")
        start_time = time.time()
        while time.time() - start_time < 30:
            status_text = page.locator(status_selector).first.evaluate(
                "el => el.childNodes[0].textContent.trim()"
            )
            if status_text.lower() != "connecting...":
                break
            print(f"  WebSocket 连接中... ({int(time.time() - start_time)}s)")
            time.sleep(2)
        else:
            print("⚠️  WebSocket 连接超时（30秒），跳过状态检查，继续执行续期。", flush=True)
            return True

        print(f"当前服务器状态: {status_text}")

        if status_text.lower() == "offline":
            print("⚠️  检测到服务器状态为 Offline，正在查找 Start 按钮...")

            start_button = page.get_by_role("button", name="Start", exact=True)

            try:
                start_button.wait_for(state='visible', timeout=10000)
            except PlaywrightTimeoutError:
                print("❌ 未找到可点击的 Start 按钮，将尝试直接续期。", flush=True)
                return True

            start_button.click()
            print("✅ 已点击 Start 按钮，等待服务器启动（最长等待 120 秒）...")

            boot_start = time.time()
            while time.time() - boot_start < 120:
                time.sleep(5)
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector(status_selector, timeout=15000)

                # reload 后等待 WebSocket 稳定，避免永远读到 "Connecting..."
                ws_wait_start = time.time()
                while time.time() - ws_wait_start < 20:
                    current_status = page.locator(status_selector).first.evaluate(
                        "el => el.childNodes[0].textContent.trim()"
                    )
                    if current_status.lower() != "connecting...":
                        break
                    time.sleep(2)

                print(f"  启动中... 当前状态: {current_status}")

                # offline 才继续等待，其余状态（starting / running 等）均视为正常
                if current_status.lower() != "offline":
                    print(f"✅ 服务器已恢复: {current_status}，继续执行续期。")
                    return True

            print("❌ 等待服务器上线超时（120秒），将尝试直接续期。", flush=True)
            page.screenshot(path="restart_timeout_error.png")
            return True

        else:
            print(f"✅ 服务器状态正常: {status_text}，无需启动。")
            return True

    except PlaywrightTimeoutError:
        print("⚠️  状态元素未在规定时间内出现，跳过状态检查，继续执行续期。", flush=True)
        return True
    except Exception as e:
        print(f"⚠️  检查服务器状态时发生错误: {e}，跳过状态检查，继续执行续期。", flush=True)
        return True
# ======================================================================


def add_time_task(page):
    """执行一次增加服务器时长的任务。"""
    try:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始执行增加时长任务...")

        if page.url != SERVER_URL:
            print(f"当前不在目标页面，正在导航至: {SERVER_URL}")
            page.goto(SERVER_URL, wait_until="domcontentloaded")

        # 续期前确认服务器状态
        ensure_server_online(page)

        add_button_selector = 'button:has-text("Add 90 minutes")'
        print("步骤1: 查找并点击 'Add 90 minutes' 按钮...")
        page.locator(add_button_selector).wait_for(state='visible', timeout=30000)
        page.locator(add_button_selector).click()
        print("...已点击 'Add 90 minutes'。")

        watch_ad_selector = 'button:has-text("Watch advertisment")'
        print("步骤2: 查找并点击 'Watch advertisment' 按钮...")
        page.locator(watch_ad_selector).wait_for(state='visible', timeout=30000)
        page.locator(watch_ad_selector).click()
        print("...已点击 'Watch advertisment'。")

        print("步骤3: 开始固定等待2分钟...")
        time.sleep(120)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ 已等待2分钟，默认任务完成。")

        return True

    except PlaywrightTimeoutError as e:
        print(f"❌ 任务执行超时: 未在规定时间内找到元素。请检查选择器或页面是否已更改。", flush=True)
        page.screenshot(path="task_element_timeout_error.png")
        return False
    except Exception as e:
        print(f"❌ 任务执行过程中发生未知错误: {e}", flush=True)
        page.screenshot(path="task_general_error.png")
        return False


def main():
    """
    主函数，执行一次登录和一次任务，然后退出。
    """
    print("启动自动化任务（单次运行, 固定等待模式）...", flush=True)

    socks5_proxy = os.environ.get('SOCKS5_PROXY')
    launch_args = []
    if socks5_proxy:
        local_proxy = "socks5://127.0.0.1:1080"
        launch_args = [f"--proxy-server={local_proxy}"]
        print(f"已启用 SOCKS5 代理，浏览器出口: {local_proxy}")
    else:
        print("未启用代理，使用 GitHub Actions 默认出口 IP。")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=launch_args)
        page = browser.new_page()
        page.set_default_timeout(60000)
        print("浏览器启动成功。", flush=True)

        try:
            if not verify_proxy_ip(page):
                print("代理 IP 验证失败，程序终止。", flush=True)
                exit(1)

            if not login_with_playwright(page):
                print("登录失败，程序终止。", flush=True)
                exit(1)

            if os.name != 'nt':
                signal.alarm(TASK_TIMEOUT_SECONDS)

            print("\n----------------------------------------------------")
            success = add_time_task(page)

            if os.name != 'nt':
                signal.alarm(0)

            if success:
                print("本轮任务成功完成。", flush=True)
            else:
                print("本轮任务失败。", flush=True)
                exit(1)

        except TaskTimeoutError as e:
            print(f"🔥🔥🔥 任务强制超时（{TASK_TIMEOUT_SECONDS}秒）！🔥🔥🔥", flush=True)
            print(f"错误信息: {e}", flush=True)
            page.screenshot(path="task_force_timeout_error.png")
            exit(1)
        except Exception as e:
            print(f"主程序发生严重错误: {e}", flush=True)
            page.screenshot(path="main_critical_error.png")
            exit(1)
        finally:
            print("关闭浏览器，程序结束。", flush=True)
            browser.close()

if __name__ == "__main__":
    main()
    print("脚本执行完毕。")
    exit(0)
