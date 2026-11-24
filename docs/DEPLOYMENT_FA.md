# استقرار روی VPS

## نصب خودکار

نصب‌کننده برای Ubuntu 24.04 LTS نوشته شده و Python 3.12 را از بسته‌های سیستم نصب می‌کند. دامنه باید به IP سرور اشاره کند و پورت‌های SSH و HTTPS در دسترس باشند. برای نسخه‌های دیگر Debian/Ubuntu، ابتدا وجود بسته‌های `python3.12` و `python3.12-venv` را بررسی کنید.

اگر مخزن GitHub خصوصی است، پیش از اجرای نصب‌کننده یک Deploy Key فقط‌خواندنی بسازید و کلید عمومی را به همان repository اضافه کنید. سپس کلید خصوصی را از مسیر امن به VPS منتقل و برای کاربر سرویس نصب کنید؛ کلید را در خود مخزن یا فرمان shell قرار ندهید:

```bash
sudo useradd --system --home-dir /opt/projecthub --create-home --shell /usr/sbin/nologin projecthub
sudo install -d -o projecthub -g projecthub -m 0700 /opt/projecthub/.ssh
sudo install -o projecthub -g projecthub -m 0600 /path/to/deploy_key /opt/projecthub/.ssh/id_ed25519
```

کلید میزبان GitHub را از منبع رسمی و با fingerprint منتشرشدهٔ GitHub بررسی کنید. بعد از تطبیق fingerprint، کلید میزبان را ثبت و دسترسی را پیش از نصب بررسی کنید:

```bash
sudo -u projecthub ssh-keyscan -t ed25519 github.com | sudo tee -a /opt/projecthub/.ssh/known_hosts >/dev/null
sudo chown projecthub:projecthub /opt/projecthub/.ssh/known_hosts
sudo chmod 0600 /opt/projecthub/.ssh/known_hosts
sudo -u projecthub git ls-remote git@github.com:OWNER/REPOSITORY.git refs/heads/main
```

نشانی بالا را با SSH URL مخزن خودتان جایگزین کنید. خروجی باید commit شاخهٔ `main` را نشان دهد. از Personal Access Token در URL یا history ترمینال استفاده نکنید.

```bash
sudo bash scripts/install-vps.sh <git-repo-url> <domain>
```

نصب‌کننده این کارها را انجام می‌دهد: PostgreSQL محلی می‌سازد، کاربر سرویس غیرممتاز می‌سازد، venv نصب می‌کند، secretهای قوی تولید می‌کند، `.env` را از Git بیرون نگه می‌دارد، Caddy را برای HTTPS تنظیم می‌کند، migration را اجرا می‌کند و کد فعال‌سازی مالک را فقط یک بار چاپ می‌کند.

کد فعال‌سازی را در:

```text
https://<domain>/admin/activate
```

وارد کنید. سپس فایل توکن‌ها را ویرایش کنید:

```bash
sudo nano /etc/projecthub/projecthub.env
```

مقدار Telegram/Bale token و username را بدون `@` تنظیم کنید. توکن‌ها را در پیام‌رسان عمومی، مخزن، issue یا لاگ نگذارید. سپس:

```bash
sudo systemctl restart projecthub-telegram projecthub-bale
sudo -u projecthub /opt/projecthub/venv/bin/python -m app.cli check-bots
```

اگر فقط یک پیام‌رسان لازم است، توکن پیام‌رسان دیگر را خالی بگذارید. API و worker از همان env استفاده می‌کنند.

## به‌روزرسانی

ابتدا تغییرات را در GitHub merge کنید. سپس روی VPS:

```bash
sudo bash /opt/projecthub/app/scripts/deploy.sh
```

اسکریپت روی checkout کثیف اجرا نمی‌شود؛ این کار مانع حذف تغییرات دستی می‌شود. از `git reset --hard` یا حذف volume/database در روند deploy استفاده نمی‌کند. migration پیش از restart اعمال می‌شود.

## سرویس‌ها و لاگ

```bash
sudo systemctl status projecthub-api projecthub-worker projecthub-telegram projecthub-bale
sudo journalctl -u projecthub-api -u projecthub-worker -n 200 --no-pager
sudo journalctl -u projecthub-telegram -u projecthub-bale -f
```

پورت 8000 فقط روی `127.0.0.1` باز است؛ Caddy TLS را فراهم می‌کند. پنل پشت reverse proxy باید Host عمومی همان `PUBLIC_BASE_URL` را بفرستد. Uvicorn access log خاموش است تا pathهای موقتی invite در journal نمانند.

## نصب دستی یا توسعه

پایتون 3.12 و PostgreSQL نیاز است. `.env.example` را کپی و مقادیر production را کامل کنید؛ `SECRET_KEY` حداقل ۳۲ بایت تصادفی و `PUBLIC_BASE_URL` باید HTTPS باشد. سپس:

```bash
python -m pip install -e .
python -m app.cli migrate
python -m app.cli issue-owner-code
```

برای محیط توسعه، optional dependencyهای `.[dev]` شامل pytest، ruff و aiosqlite هستند. سرور وب، هر poller و worker را در process جدا اجرا کنید. SQLite را برای چند process تولیدی به کار نبرید.

## پشتیبان‌گیری و بازیابی

نصب خودکار `age` را فراهم می‌کند. کلید عمومی را از محیط امن تنظیم کنید:

```bash
sudo BACKUP_RECIPIENT='age1...' bash /opt/projecthub/app/scripts/backup.sh
```

فایل `.age` هم dump دیتابیس transcript و هم فایل‌های upload را در خود دارد. کلید خصوصی را جدا از VPS نگه دارید. قبل از بازگردانی، سرویس‌ها را متوقف کنید، archive را با کلید خصوصی رمزگشایی کنید و dump/file volume را طبق عملیات استاندارد PostgreSQL بازیابی کنید؛ recovery script خودکار ارائه نشده تا اشتباهاً دادهٔ زنده جایگزین نشود.

## پایش اولیه

- `GET /healthz` زنده بودن پردازه و `GET /readyz` اتصال دیتابیس را بررسی می‌کند.
- فضای دیسک `/var/lib/projecthub/uploads` و `/var/backups/projecthub` را مانیتور کنید.
- وضعیت queue/outbox را در پنل و journal بررسی کنید.
- قبل از استفادهٔ واقعی، اتصال واقعی هر توکن و ارسال/دریافت فایل در همان پیام‌رسان را به‌صورت کنترل‌شده بررسی کنید.
