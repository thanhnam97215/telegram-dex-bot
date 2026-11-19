# DEX Bot — chỉ báo token theo chain trên Telegram

Bot Telegram theo dõi token DEX theo từng chain, dùng API miễn phí
(GeckoTerminal + DEX Screener, không cần key).

## Cài đặt (chạy trên máy của bạn)

Yêu cầu: Python 3.10+, `pip install requests`, `curl`.

```bash
git clone https://github.com/thanhnam97215/telegram-dex-bot.git
cd telegram-dex-bot
cp config.example.json config.json
chmod 600 config.json
```

1. Tạo bot qua [@BotFather](https://t.me/BotFather) trên Telegram, lấy token,
   dán vào `telegram_token` trong `config.json`.
2. Mở Telegram, gửi `/start` cho bot của bạn.
3. Lấy chat id: `python3 dexbot.py chatid`, rồi ghi số đó vào `chat_id`
   trong `config.json`.
4. Kiểm tra: `python3 dexbot.py test` (gửi 1 tin nhắn thử), hoặc
   `python3 dexbot.py report --dry-run` để xem báo cáo mà không gửi.
5. Chạy liên tục: `python3 dexbot.py run`, hoặc cài service systemd:
   `bash install.sh` (cần sudo; chạy trên máy chủ Linux có systemd).

Trên máy chủ bình thường có internet trực tiếp thì không cần cấu hình
proxy gì thêm. `config.json` chứa token nên đừng commit file này
(đã có trong `.gitignore`).

## Chức năng

- **Báo cáo đầu ngày (08:00 +07):** top 10 tăng mạnh nhất + top 10 giảm
  mạnh nhất 24h trên từng chain — một tin nhắn riêng cho mỗi chain.
  (Universe: các pool đang hot trên GeckoTerminal, lọc thanh khoản
  ≥ $1.000 và volume 24h ≥ $5.000.)
- **Cảnh báo biến động ≥ 40%:** quét mỗi 10 phút, báo khi token tăng/giảm
  ≥ 40% trong 1h hoặc 24h. Mỗi token mỗi chiều chỉ báo 1 lần/ngày.
- **Token mới list / sắp list:** pool mới tạo trên GeckoTerminal (chỉ báo
  pool tạo trong 2h qua) + token profile mới và token mới được boost trên
  DEX Screener (tín hiệu marketing/cộng đồng trước và quanh lúc list).
  Lưu ý: không API công khai nào thấy được token trước khi pair của nó
  tồn tại — đây là tín hiệu sớm nhất có thể qua dữ liệu công khai.
- **Lệnh:** `/gainers [chain]`, `/losers [chain]`, `/new [chain]`,
  `/chains`, `/status`, `/help`. Không ghi chain thì mặc định BSC.

## Chain

ETH, BSC, Solana, Base, Arbitrum (đủ dữ liệu qua GeckoTerminal) +
Robinhood Chain (GeckoTerminal chưa hỗ trợ nên chỉ dùng dữ liệu
profile/boost của DEX Screener cho chain này).

## Bảo mật

- Token Telegram có 2 cách cấp: dán thẳng vào `telegram_token` trong
  `config.json` (cách thường khi tự chạy), hoặc để trống và dùng
  `telegram_credential` — bot lấy surrogate từ Secure Vault qua
  `dynamic_credentials.py`, token thật không bao giờ nằm trong file, log
  hay code. `config.json` để mode 600 và đừng commit.
- `config.json` để mode 600. Service đọc proxy egress + CA bundle từ
  `/etc/environment` (do runtime cấp), không copy secret nào vào unit.

## File

- `dexbot.py` — toàn bộ bot (chạy `run` | `report` | `poll` |
  `commands` | `test`, thêm `--dry-run` để in ra thay vì gửi).
- `config.json` (mode 600, không commit) — token Telegram, chat id,
  chain, ngưỡng. Token chỉ nằm ở đây, không ghi vào log hay code.
- `config.example.json` — mẫu config để copy thành `config.json`.
- `state.json` — trạng thái đã thấy/đã báo (ghi atomic, tự cắt sau 48h).
- `dexbot.service` — unit systemd gốc (bản `/etc` chỉ là bản copy).
- `install.sh` — cài/cài lại service (kiểm tra config trước khi chạy).
- `bot.log` — log xoay vòng (1 MB × 3).

## Vận hành

- Service: `sudo systemctl status|start|stop|restart dexbot`
- Cài lại sau khi VM bị thay: `bash install.sh`
- Cron khôi phục `dexbot-watch` (mỗi giờ) kiểm tra service còn chạy
  không, nếu không thì chạy lại `install.sh`.
- Sửa ngưỡng/chain/giờ báo cáo: sửa `config.json` rồi
  `sudo systemctl restart dexbot`.

## Thêm chain mới

Thêm một mục vào `chains` trong `config.json`:
`{"id": "...", "label": "...", "gecko": "<id GeckoTerminal>" hoặc null,
"dex": "<chainId DEX Screener>"}`. Nếu chain chưa có trên GeckoTerminal
thì để `gecko` là null — bot vẫn báo profile/boost và token mới qua
DEX Screener.
