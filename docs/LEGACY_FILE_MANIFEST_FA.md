# فهرست فایل‌به‌فایل سورس قدیمی

این پیوست، مسیر و جمع‌بندی همهٔ فایل‌های کاربردی داخل بستهٔ ZIP ارسالی را ثبت می‌کند. ZIP در مجموع ۱۸۱ entry داشت: ۹۴ فایل پروژه و ۳۱ artifact موقت pytest (۱۲ lock و ۱۹ فایل test output با نام تصادفی)؛ باقی entryها پوشه بودند. فایل‌های موقت آزمون، فایل اجرایی پروژه نیستند و به‌صورت گروهی در انتها آمده‌اند. ZIP اصلی بدون تغییر باقی ماند.

## پیکربندی، اجرا و migration

| مسیر | جمع‌بندی بررسی |
|---|---|
| `.dockerignore` | فایل‌های محلی و بخشی از تنظیم‌ها را از Docker build context حذف می‌کرد؛ حذف secret از خود ZIP/Git را تضمین نمی‌کند. |
| `.env` | شامل مقدارهای محرمانهٔ واقعی‌نما برای کلید برنامه، پایگاه‌داده و tokenهای پیام‌رسان بود؛ مقدارها بازنشر نشدند و باید rotate شوند. |
| `.env.example` | نمونهٔ پیکربندی محیط؛ پیش‌فرض‌ها و مراحل راه‌اندازی production به توضیح بیشتری نیاز داشت. |
| `.gitignore` | env و بخشی از فایل‌های محلی را نادیده می‌گرفت؛ cache تست و بعضی artifactها را کامل پوشش نمی‌داد. |
| `Dockerfile` | ساخت image Python و اجرای non-root؛ با هدف نهایی systemd/VPS کاربر هم‌مسیر نبود. |
| `LICENSE` | مجوز پروژه موجود بود؛ بررسی حقوقی مالکیت و license وابستگی‌ها انجام نشد. |
| `Makefile` | فرمان‌های نصب، Docker، migration، تست و smoke را تجمیع می‌کرد؛ بخشی از فرمان‌ها به ساختار قبلی وابسته بودند. |
| `README_FA.md` | راهنمای اجرا و قابلیت‌های متعدد legacy؛ برای جریان سادهٔ دریافت پروژه نیاز به بازنویسی داشت. |
| `alembic.ini` | تنظیمات runner مهاجرت. |
| `alembic/env.py` | اتصال Alembic به مدل‌ها و اجرای ناهمگام migration. |
| `alembic/script.py.mako` | قالب تولید revision جدید. |
| `alembic/versions/8d4e1774499b_initial_schema.py` | طرح اولیهٔ شامل پروژه، اعتبار، پرداخت، ticket و مدیریت؛ دامنهٔ داده از نسخهٔ فعلی بزرگ‌تر بود. |
| `alembic/versions/314502d3009b_add_project_work_logs.py` | migration جدول زمان/کار پروژه؛ در جریان MVP جدید مصرفی نداشت. |
| `alembic/versions/e6c9af4f88b1_add_manual_payment_receipts.py` | migration رسید پرداخت دستی؛ در MVP جدید کنار گذاشته شد. |
| `docker-compose.yml` | اجرای PostgreSQL، Redis، API، دو bot و worker؛ runtime قبلی به Docker/Redis گره خورده بود. |
| فایل SQLite محلی قبلی | SQLite حدود ۵۱۶KB؛ schema بررسی شد و رکورد واقعی کاربر، پروژه، پیام یا پرداخت پیدا نشد، داده‌ها seed بودند. |
| `pyproject.toml` | وابستگی‌ها و پیکربندی Python؛ هم‌پوشانی بخشی از dependency list با requirements وجود داشت. |
| `requirements.txt` | فهرست دوم وابستگی‌های نصب؛ نگهداری دو فهرست ریسک اختلاف نسخه ایجاد می‌کند. |
| `data/uploads/.gitkeep` | نگه‌دارندهٔ مسیر upload خالی؛ محتوای واقعی این مسیر نباید commit شود. |

## کد برنامه

| مسیر | جمع‌بندی بررسی |
|---|---|
| `app/__init__.py` | marker بسته، بدون رفتار اجرایی. |
| `app/main.py` | برنامهٔ FastAPI، middleware و health؛ ساخت خودکار schema با Alembic هم‌پوشانی داشت. |
| `app/cli.py` | ابزار migration، راه‌اندازی/reset مدیر و بررسی اتصال؛ بعضی خروجی‌های عملیاتی secret یک‌بارمصرف داشتند. |
| `app/bot_entry.py` | entry point poller با انتخاب Telegram یا Bale. |
| `app/worker_entry.py` | entry point اجرای صف‌های پس‌زمینه. |
| `app/admin/router.py` | routeهای auth، پروژه، تیم، پرداخت و مدیریت در یک فایل حدود ۱۲۴۷ خطی؛ separation of concerns دشوار بود. |
| `app/bots/__init__.py` | marker بستهٔ bot. |
| `app/bots/flows.py` | منطق مشتری/مدیر در فایل حدود ۱۳۱۳ خطی؛ پرداخت، referral و workflowهای legacy در یک ماژول انباشته شده بودند. |
| `app/bots/runtime.py` | long polling؛ رسیدگی به رویدادها ترتیبی بود و concurrency/backpressure محدود داشت. |
| `app/bots/transport.py` | ارتباط Telegram/Bale و file/provider API؛ مرز platformها باید با قرارداد تست شود. |
| `app/core/config.py` | خواندن environment؛ کلید ثابت پیش‌فرض برای production معتبر شناخته می‌شد. |
| `app/core/jalali.py` | parse/اعتبارسنجی/نمایش تاریخ شمسی. |
| `app/core/rate_limit.py` | rate limit مبتنی بر Redis و fallback محلی؛ fallback بین چند process هماهنگ نبود. |
| `app/core/security.py` | hash رمز و token/CSRF؛ سیاست secret و محیط production سخت‌گیری کافی نداشت. |
| `app/core/time.py` | ساعت تهران، SLA و محاسبات زمانی. |
| `app/db/base.py` | Declarative Base مدل‌های SQLAlchemy. |
| `app/db/models.py` | مدل‌های مشتری، پروژه، پرداخت و wallet؛ بعضی ستون‌های مبلغ برای بیشینهٔ ورودی ۳۲بیتی بودند. |
| `app/db/session.py` | engine/session factory؛ تنظیم pool و انضباط session برای taskهای موازی باید بازبینی می‌شد. |
| `app/domain/states.py` | وضعیت‌ها و انتقال‌های پروژه و نقش‌ها. |
| `app/payments/providers.py` | adapter و تنظیم providerهای پرداخت Telegram/Bale؛ خارج از دامنهٔ شروع فعلی. |
| `app/payments/service.py` | ساخت/تأیید invoice و ثبت پرداخت؛ نیازمند آزمون قراردادی مستقل در صورت بازگشت. |
| `app/services/account_link.py` | اتصال حساب‌های مشتری با کد؛ با دعوت لینک‌محور عضو تیم یکسان نیست. |
| `app/services/analyzer.py` | analyzer قاعده‌محور؛ ارجاع مصرف‌کننده‌ای در سورس پیدا نشد. |
| `app/services/files.py` | نام امن و MIME/ذخیره؛ فایل کامل پیش از کنترل اندازه می‌توانست در حافظه قرار بگیرد. |
| `app/services/projects.py` | پروژه، wallet، referral، پرداخت، SLA و تنظیمات در سرویس حجیم؛ commitهای درونی مرز تراکنش را مبهم می‌کردند. |
| `app/workers/__init__.py` | marker بستهٔ worker. |
| `app/workers/runner.py` | زمان‌بندی task، SLA و outbox؛ claim چند worker به قفل/تضمین اتمیک نیاز داشت. |

## رابط و قالب‌های HTML

| مسیر | جمع‌بندی بررسی |
|---|---|
| `app/static/admin.css` | تم روشن آبی/سفید و RTL؛ دو تم گرادیانی خواسته‌شده را نداشت. |
| `app/templates/base.html` | قاب عمومی، منو و خروج؛ نمایش لینک‌ها برای همهٔ نقش‌ها کاملاً تفکیک نشده بود. |
| `app/templates/login.html` | فرم ورود مدیر. |
| `app/templates/dashboard.html` | کارت‌ها و شاخص‌های پروژه/مالی. |
| `app/templates/projects.html` | فهرست پروژه و فیلترهای مدیریتی. |
| `app/templates/project_detail.html` | جزئیات، پیوست، status و بخشی از timeline. |
| `app/templates/inbox.html` | صندوق پیام/اعلان تیم. |
| `app/templates/ticket_detail.html` | صفحهٔ جزئیات پشتیبانی. |
| `app/templates/team.html` | مدیریت همکاران با ورود دستی حساب؛ invitation لینک‌دار نبود. |
| `app/templates/profile_inline.html` | حساب و ویرایش رمز/پروفایل. |
| `app/templates/table.html` | جدول عمومی قابل استفادهٔ مجدد. |
| `app/templates/categories.html` | مدیریت دسته‌ها؛ نیاز جاری دسته‌بندی رشته‌ای را حذف می‌کند. |
| `app/templates/packages.html` | بسته‌های اعتبار متصل به wallet قدیمی. |
| `app/templates/payments.html` | نمایش پرداخت‌ها/رسیدها. |
| `app/templates/services.html` | فهرست خدمات آماده به‌جای ثبت درخواست آزاد. |
| `app/templates/settings.html` | تنظیم SLA، نرخ، اولویت و template پیام. |
| `app/templates/wallet.html` | موجودی و اعتبار داخلی؛ خارج از MVP دریافت پروژه. |

## مستندات قدیمی

| مسیر | جمع‌بندی بررسی |
|---|---|
| `docs/ADMIN_GUIDE_FA.md` | راهنمای نقش‌ها و پنل قدیمی. |
| `docs/ARCHITECTURE.md` | معماری قبلی bot، API، پایگاه‌داده و Docker. |
| `docs/BOT_FLOWS.md` | سناریوهای flow شامل قابلیت‌های پرداخت و referral. |
| `docs/DATA_MODEL.md` | شرح entityهای پروژه، پرداخت، wallet و ticket. |
| `docs/DEPLOYMENT_FA.md` | deployment قدیمی Docker/Compose و TLS. |
| `docs/DOCKER_DEPENDENCY_FIX_FA.md` | رفع خطای build وابستگی Docker؛ مختص محیط قبلی. |
| `docs/PAYMENT_SETUP.md` | تنظیم Telegram Stars و Bale Wallet. |
| `docs/RUNFLARE_DNS_FIX_FA.md` | راهنمای DNS و mirror وابستگی. |
| `docs/SECURITY.md` | توضیح header، cookie، CSRF و backup؛ محدودیت retention کامل روشن نبود. |
| `docs/SHAMSI_AND_CLEAN_CHAT_FA.md` | تاریخ شمسی و حذف پیام پس از نگهداری؛ جدایی آرشیو سرور و صفحهٔ کاربر مبنای طراحی تازه شد. |
| `docs/SIMPLE_WIZARD_REFERRAL_FA.md` | ساده‌سازی wizard و سیاست referral؛ نیاز به حذف قابلیت‌های غیرضروری داشت. |
| `docs/START_MESSAGE_FIX_FA.md` | رفع رفتار `/start` و تفاوت send/edit در چت. |
| `docs/TEST_REPORT.md` | گزارش snapshot قدیمی ۳۰ تست؛ تغییرهای بعدی ZIP را کامل پوشش نمی‌داد. |

## تست و عملیات

| مسیر | جمع‌بندی بررسی |
|---|---|
| `scripts/backup.sh` | backup DB و uploadها در Docker؛ نسخهٔ قبلی رمزگذاری نداشت. |
| `scripts/restore.sh` | جایگزینی DB/volume و توقف سرویس‌ها؛ عملیات مخرب و Dockerمحور. |
| `scripts/setup.py` | تولید/آماده‌سازی `.env` و تنظیم‌های local. |
| `scripts/smoke_test.py` | smoke test با fake transport و ASGI. |
| `tests/conftest.py` | fixture دیتابیس و session تست. |
| `tests/fakes.py` | fake providerها. |
| `tests/contract/test_bale_adapter.py` | قرارداد Bale، حذف پیام و جلوگیری از چاپ URL tokenدار. |
| `tests/e2e/test_project_intake.py` | ثبت درخواست و ماندگاری state پس از restart. |
| `tests/integration/test_admin_auth.py` | ورود، CSRF، نشست و بخشی از wallet. |
| `tests/integration/test_bot_flow.py` | flow bot، فایل، پرداخت، referral، edit/delete و state. |
| `tests/integration/test_http_contract.py` | health، ورود و headerهای وب. |
| `tests/integration/test_simplified_referrals.py` | referral/deep-link، لغو/بازگشت و حذف category picker. |
| `tests/unit/test_files_and_permissions.py` | اعتبارسنجی فایل و مجوز نقش. |
| `tests/unit/test_jalali.py` | اعتبارسنجی و نمایش تاریخ شمسی. |
| `tests/unit/test_payments_and_identity.py` | پرداخت، هویت و اتصال حساب. |
| `tests/unit/test_projects_wallet.py` | پروژه، کیف اعتبار، اولویت و پرداخت. |
| `tests/unit/test_time_and_rewards.py` | SLA و محدودیت پاداش. |
| `tests/unit/test_worker.py` | retry/backoff و Retry-After. |

سورس قدیمی اجرا نشد تا tokenهای ZIP و سرویس‌های متصل ناخواسته مصرف نشوند؛ بنابراین این نتیجه تحلیل فایل و schema است، نه تأیید عملی runtime قدیمی. توصیف‌های کد در این فهرست با یافته‌های مهم گزارش [بازبینی اصلی](LEGACY_AUDIT_FA.md) خوانده شوند.
