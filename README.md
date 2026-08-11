# Grok Auto Register Factory

Công cụ tự động đăng ký tài khoản **Grok (x.ai)** đa luồng, hỗ trợ tự động giải Captcha Cloudflare Turnstile, nhận mã xác thực OTP qua Email tạm thời (Temp Email) và tự động xuất token / upload lên hệ thống SUB2API.

---

## 📋 Mục Lục
1. [Tính Năng Nổi Bật](#-tính-năng-nổi-bật)
2. [Cài Đặt](#-cài-đặt)
3. [Cấu Hình Chi Tiết (.env)](#-cấu-hình-chi-tiết-env)
   - [Cấu Hình Giải Captcha (Local & API Trả Phí)](#1-cấu-hình-giải-captcha-turnstile)
   - [Cấu Hình Proxy (Clash & Residential Proxy)](#2-cấu-hình-proxy)
   - [Cấu Hình Email Tạm Thời (Temp Email)](#3-cấu-hình-email-tạm-thời-temp-email)
   - [Cấu Hình Upload Token lên SUB2API](#4-cấu-hình-upload-token-lên-sub2api)
4. [Hướng Dẫn Sử Dụng (Cách Chạy)](#-hướng-dẫn-sử-dụng-cách-chạy)
   - [Các Tham Số Lệnh (CLI Arguments)](#các-tham-số-dòng-lệnh)
   - [Ví Dụ Chạy Thực Tế](#ví-dụ-lệnh-chạy)
5. [Cấu Trúc Kết Quả Đầu Ra (Output)](#-cấu-trúc-kết-quả-đầu-ra-output)

---

## 🚀 Tính Năng Nổi Bật

- **Tự động giải Captcha Turnstile**:
  - Hỗ trợ **Local Captcha Solver** miễn phí bằng Playwright (không tốn tiền API).
  - Tích hợp 3 dịch vụ giải Captcha trả phí hàng đầu: **YesCaptcha**, **CapSolver**, **EzCaptcha**.
  - Cơ chế tự động chuyển đổi (fallback): Nếu Local giải thất bại sẽ tự động gọi sang API trả phí.
- **Quản lý Proxy đa dạng**:
  - Hỗ trợ **Clash Proxy** (tự động xoay IP / xoay node).
  - Hỗ trợ **Residential / Direct Proxy** (IPv4 / IPv6 / SOCKS5 / HTTP Proxy riêng).
- **Hệ thống Email Tạm Thời linh hoạt**:
  - Hỗ trợ các provider: `gptmail` (có key test miễn phí), `yyds`, `moemail`, `cfmail`.
  - Tự động đổi Email nếu domain bị x.ai từ chối hoặc không nhận được OTP.
- **Xuất dữ liệu & Tích hợp SUB2API**:
  - Xuất định dạng `email|refresh_token` vào thư mục `data/`.
  - Lưu đầy đủ session vào thư mục `tokens/grok/<email>/auth.json`.
  - Tự động upload Token lên panel **SUB2API** nếu có cấu hình.

---

## 📦 Cài Đặt

### 1. Clone repository
```bash
git clone https://github.com/dichvuright/grok-reg-factory.git
cd grok-reg-factory
```

### 2. Cài đặt các thư viện Python
```bash
pip install -r requirements.txt
```

### 3. Cài đặt Playwright Chromium (Nếu dùng Local Captcha miễn phí)
```bash
python -m playwright install chromium
```

---

## ⚙️ Cấu Hình Chi Tiết (.env)

Sao chép file `.env.example` thành `.env` để tiến hành cài đặt:
```bash
cp .env.example .env
```

### 1. Cấu Hình Giải Captcha (Turnstile)

Hệ thống ưu tiên sử dụng **LOCAL_CAPTCHA** (miễn phí). Nếu `LOCAL_CAPTCHA=false` hoặc giải thất bại, hệ thống sẽ lần lượt kiểm tra và gọi API key của **YesCaptcha** -> **CapSolver** -> **EzCaptcha**.

#### Cách A: Dùng Local Captcha Solver (Miễn phí, chạy bằng trình duyệt Chromium thật)
```ini
# Bật/Tắt Local Captcha (true/false)
LOCAL_CAPTCHA=true

# Chạy ẩn trình duyệt (true) hoặc hiện giao diện trình duyệt để debug (false)
LOCAL_CAPTCHA_HEADLESS=true

# Thời gian chờ giải captcha tối đa (giây)
LOCAL_CAPTCHA_TIMEOUT=30

# Số lần thử lại nếu thất bại
LOCAL_CAPTCHA_RETRIES=3
```

#### Cách B: Dùng API Giải Captcha Trả Phí (Qua bên thứ 3)
Chỉ cần điền API Key tương ứng với dịch vụ bạn sử dụng:

- **YesCaptcha** ([yescaptcha.com](https://yescaptcha.com)):
  ```ini
  YESCAPTCHA_API_KEY=your_yescaptcha_api_key
  YESCAPTCHA_API_BASE=https://api.yescaptcha.com
  ```

- **CapSolver** ([capsolver.com](https://capsolver.com)):
  ```ini
  CAPSOLVER_API_KEY=your_capsolver_api_key
  ```

- **EzCaptcha** ([ez-captcha.com](https://ez-captcha.com)):
  ```ini
  EZCAPTCHA_API_KEY=your_ezcaptcha_api_key
  EZCAPTCHA_API_BASE=https://api.ez-captcha.com
  ```

---

### 2. Cấu Hình Proxy

Hệ thống hỗ trợ 3 chế độ Proxy thông qua biến `PROXY_MODE`:

#### Chế độ 1: Dùng Clash Proxy (Tự động xoay IP / xoay Node)
Yêu cầu bạn đang bật ứng dụng Clash (như Clash Verge, Clash for Windows, Clash Party):
```ini
PROXY_MODE=clash_auto
CLASH_PROXY=http://127.0.0.1:7890
CLASH_API=http://127.0.0.1:9097
CLASH_SECRET=
```
*Ghi chú*: Nếu muốn cố định 1 Node cụ thể trong Clash:
```ini
PROXY_MODE=clash_fixed
CLASH_FIXED_NODE=Tên_Node_Trong_Clash
```

#### Chế độ 2: Dùng Proxy Tĩnh / Residential Proxy Mua Bên Ngoài
Dùng trực tiếp đường dẫn HTTP/HTTPS hoặc SOCKS5 Proxy:
```ini
PROXY_MODE=residential
REG_FACTORY_PROXY=http://user:pass@ip:port
# Hoặc hỗ trợ SOCKS5:
# REG_FACTORY_PROXY=socks5://user:pass@ip:port
```

#### Chế độ 3: Không dùng Proxy (Kết nối trực tiếp)
```ini
PROXY_MODE=none
```

---

### 3. Cấu Hình Email Tạm Thời (Temp Email)

Chọn 1 trong 4 nhà cung cấp email tạm thời qua biến `TEMP_EMAIL_PROVIDER`:

```ini
# Lựa chọn: gptmail | yyds | moemail | cfmail
TEMP_EMAIL_PROVIDER=gptmail
```

- **GPTMail** (Khuyên dùng, hỗ trợ key miễn phí `gpt-test`):
  ```ini
  GPTMAIL_BASE_URL=https://mail.chatgpt.org.uk
  GPTMAIL_API_KEY=gpt-test
  ```

- **YYDS Mail**:
  ```ini
  YYDS_BASE_URL=https://maliapi.215.im
  YYDS_API_KEY=your_yyds_api_key
  ```

- **MoeMail / Cloudflare Temp Mail**:
  ```ini
  MOEMAIL_BASE_URL=https://your-moemail-domain.com
  MOEMAIL_API_KEY=your_key

  CFMAIL_BASE_URL=https://your-cfmail-domain.com
  CFMAIL_ADMIN_PASSWORD=your_password
  ```

---

### 4. Cấu Hình Upload Token lên SUB2API (Tùy chọn)

Nếu bạn vận hành hệ thống **SUB2API** để quản lý token Grok tự động:

```ini
SUB2API_URL=https://sub2api.yourdomain.com
SUB2API_EMAIL=admin@yourdomain.com
SUB2API_PASSWORD=your_password
SUB2API_GROK_GROUP=grok
SUB2API_GROK_PROXY_ID=0
```

---

## 🏃 Hướng Dẫn Sử Dụng (Cách Chạy)

### Các Tham Số Dòng Lệnh

| Tham số | Viết tắt | Mặc định | Mô tả |
| :--- | :--- | :--- | :--- |
| `--count` | `-n` | `1` | Số lượng tài khoản Grok muốn đăng ký |
| `--workers` | `-w` | `1` | Số luồng chạy đồng thời (Multi-threading) |
| `--provider` | | từ `.env` | Ghi đè nhà cung cấp Temp Email (`gptmail`, `yyds`, ...) |
| `--sub2api` | | `False` | Tự động upload SSO Token lên SUB2API sau khi đăng ký thành công |
| `--sub2api-group` | | `grok` | Nhóm (group) trên SUB2API |
| `--mailbox-attempts`| | `6` | Số lần thử đổi Email nếu domain bị từ chối/không nhận OTP |
| `--code-timeout` | | `75` | Thời gian tối đa (giây) chờ mã OTP gửi về Mailbox |

---

### Ví Dụ Lệnh Chạy

#### 1. Đăng ký 1 tài khoản đơn lẻ (dùng để test)
```bash
python register_grok.py
```

#### 2. Đăng ký 10 tài khoản chạy đơn luồng
```bash
python register_grok.py --count 10
```

#### 3. Đăng ký 20 tài khoản chạy đa luồng (3 luồng song song)
```bash
python register_grok.py --count 20 --workers 3
```

#### 4. Đăng ký 50 tài khoản và tự động đẩy Token lên SUB2API
```bash
python register_grok.py --count 50 --workers 5 --sub2api
```

#### 5. Chọn nhanh provider Temp Email trực tiếp từ lệnh
```bash
python register_grok.py --count 5 --provider yyds
```

---

## 📂 Cấu Trúc Kết Quả Đầu Ra (Output)

Khi đăng ký thành công, kết quả sẽ được tự động lưu trữ tại 2 nơi:

1. **File Tổng hợp Định Dạng TXT**:
   - Đường dẫn: `data/grok_YYYYMMDD_HHMMSS_count.txt`
   - Định dạng từng dòng:
     ```text
     email_dang_ky@domain.com|refresh_token_grok_oauth...
     ```

2. **File Session Chi Tiết (JSON)**:
   - Đường dẫn: `tokens/grok/<email_dang_ky>/auth.json`
   - Chứa toàn bộ Cookie, Access Token, Refresh Token và SSO Session để tái sử dụng hoặc export.
