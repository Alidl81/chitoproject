# استقرار ChitoProject

## به‌روزرسانی نصب موجود روی VPS

سرور این نصب Ubuntu 26.04.1 است. مخزن GitHub در `/opt/chitoproject-repo`، برنامه در `/opt/chitoproject/app`، پایتون محیط مجازی با uv در `/opt/chitoproject-python` و فایل تنظیمات در `/etc/chitoproject/chitoproject.env` قرار دارند. دیتابیس و بارگذاری‌ها زیر `/var/lib/chitoproject` هستند. در VPS فعلی، پورت 443 متعلق به Xray است و Caddy روی 9443 سرویس می‌دهد؛ نشانی پنل `https://46.249.107.214:9443/admin` است.

برای این نصب موجود، `install-vps.sh` را اجرا نکنید. آن اسکریپت فقط برای نصب تازه است و عمداً در حضور مسیرها، سرویس‌ها، Caddy، کلیدها یا دادهٔ موجود متوقف می‌شود.

دستور زیر، بدون اجرای نصب‌کننده، نسخهٔ `deploy.sh` را از شاخهٔ `main` با کلید Deploy موجود در `/opt/chitoproject/.ssh/id_ed25519` می‌گیرد و اجرا می‌کند. Deploy هم‌زمان اصل remote و شاخه را بررسی می‌کند، در صورت وجود فایل‌های محلی غیرمنتظره در checkout یا برنامه متوقف می‌شود و فقط fast-forward امن می‌پذیرد. محیط Python باید همان uv-managed Python 3.12.16 باشد.

```bash
sudo bash -c 'set -euo pipefail; r=/opt/chitoproject-repo; g=(git -c safe.directory="$r" -C "$r"); export GIT_SSH_COMMAND="ssh -i /opt/chitoproject/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/opt/chitoproject/.ssh/known_hosts"; "${g[@]}" fetch --no-tags git@github.com:Alidl81/chitoproject.git refs/heads/main; sha=$("${g[@]}" rev-parse FETCH_HEAD); "${g[@]}" merge-base --is-ancestor HEAD "$sha"; f=$(mktemp); trap "rm -f -- \"$f\"" EXIT; "${g[@]}" show "$sha:scripts/deploy.sh" > "$f"; bash "$f" "$sha"'
```

Deploy اسکریپت نصب تازه را اجرا نمی‌کند، پایگاه‌داده یا سرویس systemd را از نو نمی‌سازد، فایل تنظیمات را دست نمی‌زند و پوشهٔ داده و آپلود را جابه‌جا نمی‌کند. migration پیش از restart اجرا می‌شود. API، worker و ربات‌های موجود و فعال/enableشده به‌ترتیب restart می‌شوند. نسخهٔ کد قبلی در `/opt/chitoproject/.app-rollback-*` نگه داشته می‌شود. در صورت شکست، اسکریپت کد قبلی را برمی‌گرداند؛ migration افزایشیِ اعمال‌شده را downgrade نمی‌کند.

## بررسی گواهی و بازنشدن `/admin`

آدرس `/admin` در برنامه باید بدون نشست مدیریتی به `/admin/login` هدایت شود؛ مسیر `/admin/login` صفحهٔ ورود را برمی‌گرداند. `/healthz` سلامت پردازه و `/readyz` اتصال دیتابیس را بررسی می‌کنند. خطای مرورگر `ERR_CERT_COMMON_NAME_INVALID` قبل از دریافت پاسخ HTTP رخ می‌دهد؛ در آن وضعیت هنوز نمی‌توان از روی مرورگر دربارهٔ route یا دیتابیس نتیجه گرفت.

علت مشخصی که در خروجی‌های این VPS دیده شد، دو مورد بود: درخواست بدون پورت به `:443` می‌رسید، درحالی‌که `xray` پورت 443 را گرفته بود و گواهی Microsoft نام IP را پوشش نمی‌داد؛ پس از انتقال Caddy به 9443، یک خطای 502 موقت هم از `connect: connection refused` به API ثبت شد. خروجی‌های بعدی شما نشان داد API روی 8000 پاسخ می‌دهد، `/admin` کد 303 به ورود می‌دهد و `/admin/login` کد 200 می‌دهد. برای مرورگر باید نشانی دارای `:9443` را باز کنید. این تشخیص بر مبنای خروجی‌های ارائه‌شده است؛ دسترسی SSH زنده به VPS از این محیط انجام نشده است.

روی سرور، ابتدا وضعیت سرویس و پاسخ داخلی API را بررسی کنید. دستورهای HTTPS عمداً اعتبار گواهی را بررسی می‌کنند؛ از `curl -k` استفاده نکنید.

```bash
sudo systemctl status caddy chitoproject-api chitoproject-worker chitoproject-telegram chitoproject-bale --no-pager
sudo journalctl -u caddy -u chitoproject-api -u chitoproject-worker -n 120 --no-pager
sudo caddy version
sudo caddy validate --config /etc/caddy/Caddyfile
curl --fail --show-error -H 'Host: 46.249.107.214' http://127.0.0.1:8000/healthz
curl --fail --show-error -H 'Host: 46.249.107.214' http://127.0.0.1:8000/readyz
curl --fail --show-error --location https://46.249.107.214:9443/admin
curl --fail --show-error --location https://46.249.107.214:9443/admin/login
curl --fail --show-error https://46.249.107.214:9443/healthz
curl --fail --show-error https://46.249.107.214:9443/readyz
```

برای IP عمومی، [Let's Encrypt از گواهی IP پشتیبانی می‌کند](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)؛ گواهی IP فقط با پروفایل `shortlived` و اعتبار ۱۶۰ ساعت صادر می‌شود. Caddy باید نسخهٔ دارای پشتیبانی صدور برای IP را داشته باشد؛ نصب‌کننده حداقل 2.11.1 را کنترل می‌کند و تمدید خودکار Caddy باید فعال بماند. گزینهٔ `profile shortlived` و خاموش‌کردن TLS-ALPN در صورت اشغال 443 باید با دستورالعمل رسمی [TLS در Caddyfile](https://caddyserver.com/docs/caddyfile/directives/tls) تنظیم شود. برای این VPS، لاگ Caddy نشان داده بود اعتبارسنجی HTTP-01 روی پورت 80 موفق شد. این روش نیاز دارد پورت 80 از اینترنت به Caddy برسد؛ مستندات [HTTPS خودکار Caddy](https://caddyserver.com/docs/automatic-https) را ببینید.

نمونهٔ site block که باید برای این IP در پیکربندی واردشدهٔ Caddy فعال باشد:

```caddyfile
{
    http_port 80
    https_port 9443
}

http://46.249.107.214 {
    redir https://46.249.107.214:9443{uri} permanent
}

https://46.249.107.214:9443 {
    tls {
        issuer acme https://acme-v02.api.letsencrypt.org/directory {
            profile shortlived
            disable_tlsalpn_challenge
        }
    }
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
```

اگر گواهیِ ارائه‌شده IP را در `subjectAltName` ندارد، نسخهٔ Caddy پایین‌تر از حداقل نصب‌کننده است، یا در لاگ‌ها خطای ACME دیده می‌شود، ابتدا فایل واقعی `/etc/caddy/Caddyfile` و فایل‌های import شده را بررسی کنید. پیش از اصلاح، فقط فایل اختصاصی ChitoProject را پشتیبان بگیرید؛ بلوک بالا را در `/etc/caddy/conf.d/chitoproject.caddy` تنظیم کنید، سپس اعتبارسنجی و reload انجام دهید:

```bash
sudo cp -a /etc/caddy/conf.d/chitoproject.caddy "/etc/caddy/conf.d/chitoproject.caddy.bak.$(date -u +%Y%m%dT%H%M%SZ)"
sudoedit /etc/caddy/conf.d/chitoproject.caddy
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

اگر `caddy validate` خطا داد، reload نکنید؛ نسخهٔ پشتیبان را به مسیر اصلی برگردانید. فایل دادهٔ Caddy را برای گرفتن گواهی جدید پاک نکنید. سپس تا صدور و ارائهٔ گواهی معتبر، دوباره URLهای HTTPS را با `curl` عادی بررسی نکنید؛ خطا را از خروجی Caddy و وضعیت پورت‌ها پیدا کنید.

## نصب تازه

نصب‌کننده برای Ubuntu 24.04 و 26.04 است؛ به پایتون apt وابستگی ندارد. پیش از اجرا باید uv در مسیری نصب باشد که کاربر `chitoproject` بتواند آن را اجرا کند و Python 3.12.16 تحت `/opt/chitoproject-python` با uv نصب شده باشد. Python سیستم تغییر نمی‌کند. نصب‌کننده به‌محض مشاهدهٔ مسیر نصب، دیتابیس/نقش هم‌نام، Caddy یا سرویس موجود متوقف می‌شود.

در این نصب، کلید فقط‌خواندنی Deploy برای دریافت کد از VPS استفاده می‌شود و باید برای کاربر `chitoproject` قابل‌خواندن باشد؛ فایل `known_hosts` معتبر نیز باید در همان مسیر باشد. نصب‌کننده پیش از clone دسترسی همان کاربر به `main` را آزمایش می‌کند؛ اجرای clone با کاربر root کافی نیست.

نمونهٔ نصب تازه (این بخش را برای نصب فعلی اجرا نکنید):

```bash
sudo bash scripts/install-vps.sh git@github.com:Alidl81/chitoproject.git 46.249.107.214
```

نصب‌کننده ابتدا پورت‌های 80 و HTTPS انتخاب‌شده را بررسی می‌کند. اگر 443 اشغال باشد و 9443 آزاد باشد، Caddy را روی 9443 می‌گذارد؛ ورودی TCP همان پورت باید در فایروال VPS و ارائه‌دهنده باز باشد. برای IP از ACME Let's Encrypt و `profile shortlived` استفاده می‌شود و وقتی HTTPS روی پورت سفارشی است، TLS-ALPN خاموش و HTTP-01 روی پورت 80 استفاده می‌شود. پس از نصب، `/admin/activate` را فقط وقتی TLS گواهی عمومی معتبر دارد باز کنید.

## مالک اصلی و تنظیم ربات‌ها

اگر حساب مالک اصلی هنوز ساخته نشده است، از مسیر برنامه کد فعال‌سازی یک‌بارمصرف بگیرید. کد در ترمینال همان سرور چاپ می‌شود؛ آن را در گفتگو، لاگ اشتراکی یا Git قرار ندهید. این دستور در صورت وجود مالک عمداً کد تازه نمی‌سازد.

```bash
cd /opt/chitoproject/app
sudo -u chitoproject /opt/chitoproject/venv/bin/python -m app.cli issue-owner-code
```

کد را در `https://46.249.107.214:9443/admin/activate` وارد کنید (در نصب با پورت پیش‌فرض 443، `:9443` را حذف کنید). برای تنظیم توکن‌ها و نام‌کاربری‌های ربات، فایل محرمانه را با ویرایشگر امن باز کنید؛ مقدار توکن را در ترمینال، دستور history یا گفتگو کپی نکنید:

```bash
sudoedit /etc/chitoproject/chitoproject.env
```

متغیرهای `TELEGRAM_BOT_TOKEN`، `TELEGRAM_BOT_USERNAME`، `BALE_BOT_TOKEN` و `BALE_BOT_USERNAME` را با مقادیر خودتان پر کنید؛ نام‌کاربری بدون `@` است. بعد از ویرایش، سرویس‌ها را restart کنید و بدون چاپ توکن، اتصال را بررسی کنید:

```bash
sudo systemctl restart chitoproject-api chitoproject-worker chitoproject-telegram chitoproject-bale
cd /opt/chitoproject/app
sudo -u chitoproject /opt/chitoproject/venv/bin/python -m app.cli check-bots
sudo systemctl status caddy chitoproject-api chitoproject-worker chitoproject-telegram chitoproject-bale --no-pager
sudo journalctl -u caddy -u chitoproject-api -u chitoproject-worker -u chitoproject-telegram -u chitoproject-bale -n 100 --no-pager
```

## پشتیبان‌گیری و پایش

- `GET /healthz` زنده بودن پردازه و `GET /readyz` دسترسی دیتابیس را می‌سنجد.
- برای بررسی HTTPS از curl عادی استفاده کنید تا اعتبار CA و تطابق IP بررسی شود؛ خطای TLS را با گزینهٔ ناامن پنهان نکنید.
- فضای `/var/lib/chitoproject/uploads` و `/var/backups/chitoproject` و وضعیت queue/outbox را پایش کنید.
- پشتیبان رمزگذاری‌شده با `age` بسازید؛ `BACKUP_RECIPIENT` را در محیط امن تنظیم کنید:

```bash
sudo BACKUP_RECIPIENT='age1...' bash /opt/chitoproject/app/scripts/backup.sh
```

کلید خصوصی رمزگشایی را بیرون از VPS نگه دارید. بازیابی دیتابیس و بارگذاری‌ها عملیات جداگانه است و خودکار اجرا نمی‌شود تا دادهٔ زنده تصادفاً جایگزین نشود.
