# استقرار روی VPS

## نصب تازه روی Ubuntu 24.04

این دستورها برای نصب تازه هستند. نصب‌کننده در صورت وجود مسیرهای برنامه، داده، تنظیمات، سرویس یا نصب Caddy متوقف می‌شود؛ مسیرهای موجود را حذف نکنید. در صورت وجود نقش یا پایگاه‌دادهٔ PostgreSQL با نام chitoproject نیز نصب متوقف می‌شود؛ چون این بررسی پس از نصب بسته‌ها انجام می‌شود، ممکن است پیش از توقف بخشی از نصب اولیه انجام شده باشد. پیش از نصب، نسخهٔ پشتیبان و شرایط سرور را بررسی کنید. اگر نصب نیمه‌کاره شد، آن را پاک نکنید؛ ابتدا علت و داده‌های موجود را بررسی کنید.

برای مخزن خصوصی باید **کلید اختصاصی فقط‌خواندنی متعلق به کاربر سرویس** ساخته شود. بخش‌های زیر را روی VPS اجرا کنید. اگر کاربر یا فایل‌های SSH از قبل موجودند، قبل از تغییر آنها را بررسی کنید.

```bash
sudo useradd --system --home-dir /opt/chitoproject --create-home --shell /usr/sbin/nologin chitoproject
sudo install -d -m 700 -o chitoproject -g chitoproject /opt/chitoproject/.ssh
sudo -u chitoproject ssh-keygen -t ed25519 -N '' -f /opt/chitoproject/.ssh/id_ed25519 -C chitoproject-vps-read-only
sudo cat /opt/chitoproject/.ssh/id_ed25519.pub
```

فقط خروجی **کلید عمومی** را در GitHub، بخش Settings → Deploy keys → Add deploy key همان مخزن ثبت کنید و گزینهٔ write access را فعال نکنید. کلید خصوصی را هرگز ارسال نکنید. کلید عمومی میزبان GitHub را مطابق [مستندات رسمی](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints) بررسی و ذخیره کنید:

```bash
sudo sh -c 'printf "%s\n" "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" > /opt/chitoproject/.ssh/known_hosts'
sudo chown chitoproject:chitoproject /opt/chitoproject/.ssh/known_hosts
sudo chmod 600 /opt/chitoproject/.ssh/known_hosts
sudo -u chitoproject env GIT_SSH_COMMAND='ssh -i /opt/chitoproject/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/opt/chitoproject/.ssh/known_hosts' git ls-remote git@github.com:Alidl81/chitoproject.git refs/heads/main
```

در صورت وجود known_hosts از قبل، قبل از اجرای دستور اول آن را بررسی و حفظ کنید. خروجی ls-remote باید SHA شاخه را نشان دهد. نصب‌کننده و deploy هر دو همین کلید و known_hosts را صریحاً استفاده می‌کنند.

```bash
cd /opt/chitoproject-repo
git status --short
git fetch origin main
git pull --ff-only origin main
sudo bash scripts/install-vps.sh git@github.com:Alidl81/chitoproject.git 46.249.107.214
```

اگر checkout تغییر محلی دارد، ابتدا آن را حفظ کنید و خطا را بررسی کنید. نصب‌کننده روی checkout این مسیر تغییری نمی‌دهد و نسخهٔ مستقل برنامه را در `/opt/chitoproject/app` کلون می‌کند. فایل محرمانه در `/etc/chitoproject/chitoproject.env` نگهداری می‌شود. نصب مستقیم از ZIP پشتیبانی نمی‌شود.

ورودی پورت TCP 80 و 443 از اینترنت باید به VPS برسد و پورت 8000 فقط محلی است. نصب از مخزن رسمی Caddy استفاده می‌کند و حداقل Caddy 2.11.1 می‌خواهد. برای IP از issuer رسمی Let's Encrypt و پروفایل shortlived استفاده می‌کند؛ اعتبار گواهی 160 ساعت است و Caddy در حال اجرا تمدید را انجام می‌دهد. صدور به دسترسی واقعی پورت‌ها و تأیید CA وابسته است. تا بررسی موفق گواهی مرورگر، کد فعال‌سازی را وارد نکنید:

```bash
curl --fail --show-error https://46.249.107.214/readyz
sudo journalctl -u caddy -n 100 --no-pager
```

کد یک‌بارمصرف نصب تا ۲۴ ساعت اعتبار دارد. در صورت فراموشی و تا پیش از ایجاد مالک، کد تازه با دستور زیر در ترمینال خصوصی ایجاد می‌شود و کد قبلی را باطل می‌کند:

```bash
cd /opt/chitoproject/app
sudo -u chitoproject /opt/chitoproject/venv/bin/python -m app.cli issue-owner-code
```

پس از تأیید TLS، `https://46.249.107.214/admin/activate` را باز کنید، کد را وارد و حساب مالک را بسازید.

```bash
sudo nano /etc/chitoproject/chitoproject.env
```

توکن و نام کاربری هر ربات را در متغیرهای `TELEGRAM_BOT_TOKEN`، `TELEGRAM_BOT_USERNAME`، `BALE_BOT_TOKEN` و `BALE_BOT_USERNAME` وارد کنید؛ نام کاربری بدون @ است. سپس تمام سرویس‌ها را بازخوانی کنید تا پنل نیز نام‌های جدید را ببیند:

```bash
sudo systemctl restart chitoproject-api chitoproject-worker chitoproject-telegram chitoproject-bale
cd /opt/chitoproject/app
sudo -u chitoproject /opt/chitoproject/venv/bin/python -m app.cli check-bots
sudo systemctl status caddy chitoproject-api chitoproject-worker chitoproject-telegram chitoproject-bale --no-pager
sudo journalctl -u caddy -u chitoproject-api -u chitoproject-worker -u chitoproject-telegram -u chitoproject-bale -n 100 --no-pager
```

اگر فقط یک توکن تنظیم شود، سرویس مربوط به پیام‌رسان دیگر ممکن است اجرا نشود. اطلاعات محرمانه را در مخزن، اسکرین‌شات و گفتگو منتشر نکنید.

## به‌روزرسانی نصب Git

```bash
sudo bash /opt/chitoproject/app/scripts/deploy.sh
```

اسکریپت با checkout کثیف متوقف می‌شود، با کلید کاربر سرویس fetch می‌کند و تنها fast-forward انجام می‌دهد. پیش از به‌روزرسانی از داده‌ها پشتیبان بگیرید. migration پیش از restart اجرا می‌شود.

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
sudo BACKUP_RECIPIENT='age1...' bash /opt/chitoproject/app/scripts/backup.sh
```

فایل `.age` هم dump دیتابیس transcript و هم فایل‌های upload را در خود دارد. کلید خصوصی را جدا از VPS نگه دارید. قبل از بازگردانی، سرویس‌ها را متوقف کنید، archive را با کلید خصوصی رمزگشایی کنید و dump/file volume را طبق عملیات استاندارد PostgreSQL بازیابی کنید؛ recovery script خودکار ارائه نشده تا اشتباهاً دادهٔ زنده جایگزین نشود.

## پایش اولیه

- `GET /healthz` زنده بودن پردازه و `GET /readyz` اتصال دیتابیس را بررسی می‌کند.
- فضای دیسک `/var/lib/chitoproject/uploads` و `/var/backups/chitoproject` را مانیتور کنید.
- وضعیت queue/outbox را در پنل و journal بررسی کنید.
- قبل از استفادهٔ واقعی، اتصال واقعی هر توکن و ارسال/دریافت فایل در همان پیام‌رسان را به‌صورت کنترل‌شده بررسی کنید.
