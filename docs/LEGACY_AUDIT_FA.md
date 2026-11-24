# گزارش بازبینی فایل‌های سورس قبلی

## دامنه و روش

بستهٔ ارسالی ۱۸۱ مدخل ZIP داشت و ۱۲۵ فایل واقعی از آن برای بررسی استخراج شد. این بررسی ایستا بود؛ کد قبلی اجرا نشد و تست‌های آن اجرا نشدند. گزارش قدیمی خود پروژه می‌گوید ۳۰ تست در snapshot اکتبر قبول شده بود، اما چند تغییر و تست بعدی پس از آن دوباره اجرا نشده بودند. شمارش ایستای این بسته ۵۲ تعریف تابع آزمون را پیدا کرد.

کد تازه در این مخزن از نو ساخته شده است. از `.env`، دیتابیس SQLite، کش تست، فایل‌های کاربران یا مقادیر secret بستهٔ قدیمی چیزی در این مخزن کپی نشده است.

## یافته‌های مهم

| شدت | مورد | نتیجهٔ بررسی |
|---|---|---|
| بحرانی | `.env` در ZIP | شامل مقدارهای محرمانهٔ واقعی‌نما برای کلید برنامه، پایگاه‌داده و توکن پیام‌رسان بود. مقدارها در این گزارش بازنشر نمی‌شوند. باید باطل و تعویض شوند؛ حذف فایل از ZIP کافی نیست. |
| بحرانی | کلید پیش‌فرض نشست | مقدار پیش‌فرض توسعه در تولید هم با بررسی طول ۳۲ نویسه قابل قبول بود؛ این مقدار معلوم می‌توانست امضای cookie مدیریتی را جعل‌پذیر کند. |
| زیاد | چت گروهی | مسیر مشتری خصوصی‌بودن chat را enforce نمی‌کرد؛ برخی داده‌های پروژه امکان داشت در گروه نمایش داده شوند. |
| زیاد | آرشیو گفتگو | raw updateها ماندگار بودند، اما timeline کامل پیام‌های ربات/مشتری در پنل وجود نداشت؛ حذف حساب هم فقط ticket می‌ساخت. پاک‌سازی صفحه از نگه‌داری transcript مدیریتی جدا نبود. |
| زیاد | هم‌زمانی | حلقهٔ دریافت پیام‌ها را یکی‌یکی پردازش می‌کرد؛ هر `AsyncSession` برای task جدا استفاده نشده بود؛ worker چندپردازه‌ای برای outbox claim قفل اتمیک نداشت. |
| زیاد | چرخهٔ دیتابیس | startup وب‌سایت `create_all` اجرا می‌کرد، در حالی که migrationهای Alembic نیز وجود داشتند؛ چند process می‌توانستند هم‌زمان schema بسازند. |
| زیاد | مدیریت تیم | نقش‌ها وجود داشتند، اما افزودن عضو با username و password یک‌بارنمایش بود؛ invite لینک‌دار و فعال‌سازی یک‌بارمصرف مالک وجود نداشت. |
| متوسط/زیاد | محدودسازی ورود | Redis `INCR` و `EXPIRE` جدا اجرا می‌شدند؛ در خطای Redis، محدودکنندهٔ local process قابل دورزدن و به‌صورت حافظه رشدپذیر بود. |
| متوسط/زیاد | فایل‌ها | اندازه قبل از دانلود کامل بررسی می‌شد؛ داده در RAM buffer می‌شد؛ مسیر `NEEDS_INFO` حد کلی یکسانی نداشت؛ اسکن بدافزار، object storage و رمزگذاری فایل در حالت سکون موجود نبودند. |
| متوسط | پاسخ‌های بلند | جزئیات ticket تا ۲۰ پیام با سقف ۷۰۰ نویسه می‌ساخت که از سقف متن Telegram عبور می‌کرد؛ پروژه و فایل‌های دیگر نیز محدودیت ثابت خروجی نداشتند. |
| متوسط | واحد پول | فرم تا `10^12` تومان می‌پذیرفت، اما چند ستون مبلغ `Integer` سی‌ودوبیتی بودند؛ مبالغ بزرگ در Postgres می‌توانستند overflow شوند. |
| متوسط | پنل | پنل RTL، سروررندر و RBAC/CSRF داشت، ولی همهٔ لینک‌های منو در همهٔ نقش‌ها دیده می‌شدند؛ ظاهر تک‌تم آبی/سفید بود و طراحی درخواستی دارک/لایت نداشت. |
| کم/متوسط | دامنهٔ محصول | بسته‌ها، امتیاز، referrals، پرداخت، اولویت و دسته‌بندی‌ها دامنه را از دریافت پروژه بزرگ‌تر کرده بودند؛ چند فیلد و قابلیت دیگر در wizard جاری استفاده نمی‌شدند. |

### نکات مثبت سورس قبلی

- دو adapter مستقل Telegram و Bale، پنل FastAPI، worker و PostgreSQL/Redis در معماری جدا شده بودند.
- فرم Inline و پیام کنترل قابل ویرایش بود؛ چند مرحله در دیتابیس باقی می‌ماند.
- Argon2، CSRF، HttpOnly/Secure cookie در production، CSP، audit، بررسی مالکیت پروژه و بعضی idempotency keyها وجود داشتند.
- Postgres در بخش‌هایی از پرداخت/اعتبار از قفل ردیف استفاده می‌کرد؛ Docker به‌صورت non-root اجرا می‌شد و API روی loopback bind می‌شد.
- فایل‌ها کلید تصادفی می‌گرفتند و ZIP استخراج یا اجرا نمی‌شد.

## فهرست فایل‌های ریشه و عملیات

| فایل | کاربرد و ارزیابی |
|---|---|
| `.dockerignore` | حذف `.env`، venv، testها و uploadها از build context؛ مناسب Docker، اما جایگزین حذف فایل محرمانه از ZIP/Git نیست. |
| `.env` | تنظیم واقعی local شامل secretهای محرمانه؛ نباید در بستهٔ قابل‌اشتراک باشد. |
| `.env.example` | نمونهٔ تنظیمات؛ بعضی مقدارها پیش‌فرض ناامن یا توضیح‌نداده بودند. |
| `.gitignore` | `.env`، دیتابیس محلی، upload و cacheهای متداول را حذف می‌کرد؛ cache `pytest-of-root` را حذف نمی‌کرد. |
| `Dockerfile` | image پایتون ۳.۱۲، نصب وابستگی از mirror و کاربر non-root؛ مناسب مسیر Docker قبلی، نه معماری VPS درخواستی. |
| `LICENSE` | مجوز پروژه؛ بررسی حقوقی مالکیت یا انطباق وابستگی‌ها جزو این بازبینی نبود. |
| `Makefile` | فرمان‌های نصب، lint، تست، smoke، migration و bootstrap مدیر. |
| `README_FA.md` | راهنمای جامع اما Dockerمحور، شامل پرداخت، referral، تنظیمات و استفادهٔ local. |
| `alembic.ini` | پیکربندی migration. |
| `docker-compose.yml` | PostgreSQL، Redis، migration، API، دو bot و worker؛ API روی loopback. |
| `pyproject.toml`, `requirements.txt` | وابستگی‌ها و ابزارهای Python؛ Python 3.12، FastAPI، SQLAlchemy async، aiogram و httpx. |
| `projecthub.db` | دیتابیس SQLite محلی ۵۱۶KB. بازبینی schema نشان داد کاربر، پروژه، transcript، فایل، invoice و پرداخت واقعی نداشت؛ دادهٔ seed تنظیمات و دسته‌ها داشت. در سورس تازه استفاده نمی‌شود. |
| `data/uploads/.gitkeep` | نگه‌داشت پوشهٔ خالی upload. |
| `scripts/setup.py` | ساخت `.env` محلی و توکن‌های اختیاری. |
| `scripts/backup.sh` | dump دیتابیس و tar فایل‌های upload در Docker؛ backup رمزگذاری نمی‌شد. |
| `scripts/restore.sh` | توقف سرویس‌ها و جایگزینی DB/volume؛ destructive و Dockerمحور. |
| `scripts/smoke_test.py` | بررسی adapter fake، صفحه ورود و health در ASGI. |

## Migrationها

| فایل | بررسی |
|---|---|
| `alembic/env.py` | اجرای Alembic ناهمگام و بارگذاری metadata مدل‌ها. |
| `alembic/script.py.mako` | قالب تولید revision. |
| `alembic/versions/8d4e1774499b_initial_schema.py` | ساخت طرح اولیهٔ بزرگ با پروژه، پیام، اعتبار، پرداخت، دسته، بسته و داده‌های مدیریتی. |
| `alembic/versions/314502d3009b_add_project_work_logs.py` | جدول ثبت زمان کار. |
| `alembic/versions/e6c9af4f88b1_add_manual_payment_receipts.py` | جدول رسید پرداخت دستی. |

## کد برنامهٔ قبلی

| فایل | مسئولیت و ارزیابی |
|---|---|
| `app/__init__.py` | marker بسته. |
| `app/main.py` | FastAPI lifespan، schema auto-create، seed، session middleware، هدر امنیتی و health/readiness. مشکل: auto-create موازی با Alembic و پذیرش secret پیش‌فرض. |
| `app/cli.py` | migration/schema init، bootstrap/reset رمز مدیر، check botها و استعلام Bale Wallet. Bootstrap رمز موقت چاپ می‌کرد. |
| `app/bot_entry.py` | اجرای poller مستقل با آرگومان telegram/bale. |
| `app/worker_entry.py` | entrypoint worker. |
| `app/core/config.py` | تنظیمات Pydantic، env و tokenها؛ default secret ثابت برای توسعه/تولید. |
| `app/core/jalali.py` | parse/validate/format تاریخ جلالی. |
| `app/core/rate_limit.py` | rate limit ورود با Redis و fallback محلی غیرمتمرکز. |
| `app/core/security.py` | Argon2، token، digest، CSRF token و زمان UTC. |
| `app/core/time.py` | ساعت تهران، SLA و بازهٔ کاری. |
| `app/domain/states.py` | statusهای پروژه، نقش‌های admin و transitionهای ثابت. |
| `app/db/base.py` | SQLAlchemy declarative base. |
| `app/db/session.py` | engine و `AsyncSession` factory؛ pool تنظیم کمی داشت. |
| `app/db/models.py` | ۴۷۳ خط مدل‌های کاربر، پروژه، جریان اعتبار/پرداخت، تیکت، پیام، outbox و admin. مبلغ‌های بزرگ چندجا `Integer` بودند. |
| `app/admin/router.py` | حدود ۱۲۴۷ خط routeهای login، dashboard، پروژه، پرداخت، تنظیمات، کاربران، تیکت، تیم و audit؛ monolith بزرگ و منوی نقش‌ها نامتناسب با endpoint permission. |
| `app/bots/__init__.py` | marker ماژول bot. |
| `app/bots/flows.py` | حدود ۱۳۱۳ خط flow مشتری، referral، پرداخت، wizard و پنل پیام‌رسان؛ فایل بسیار بزرگ با مسیرهای legacy و conversation archive ناقص. |
| `app/bots/runtime.py` | دریافت long polling و پردازش یکی‌یکی updateها با قفل poller. |
| `app/bots/transport.py` | adapter Telegram/aiogram و Bale/httpx، inline UI، file transfer و payment calls. |
| `app/services/projects.py` | حدود ۷۶۳ خط منطق پروژه، invoice، wallet، status، SLA، referral، settings و outbox؛ commitهای داخل service تراکنش را با flow قاطی می‌کردند. |
| `app/services/files.py` | نام امن، MIME sniff و ذخیرهٔ خصوصی؛ ورودی کامل ابتدا RAM buffer می‌شد و SVG مجاز بود. |
| `app/services/account_link.py` | کد یک‌بارمصرف برای اتصال حساب‌های مشتری بین پیام‌رسان‌ها؛ دعوت همکار نبود. |
| `app/services/analyzer.py` | analyzer rule-based تعریف‌شده، اما در سورس به آن ارجاعی پیدا نشد. |
| `app/payments/providers.py` | adapterها و تنظیمات payment provider. |
| `app/payments/service.py` | ساخت و تأیید invoice، پرداخت و قواعد مالی. |
| `app/workers/__init__.py` | marker worker. |
| `app/workers/runner.py` | پردازش SLA، quote expiry، incoming retry و outbox؛ claim چندمصرف‌کننده‌ای اتمیک نبود. |

### Template و staticهای قبلی

| فایل | مسئولیت |
|---|---|
| `app/static/admin.css` | یک stylesheet RTL آبی/سفید، بدون انتخاب دارک/لایت و بدون گرادیان‌های درخواستی. |
| `app/templates/base.html` | layout مشترک، sidebar و فرم خروج. |
| `app/templates/login.html` | ورود admin. |
| `app/templates/dashboard.html` | شاخص‌های پروژه و پرداخت. |
| `app/templates/projects.html` | فهرست و فیلتر پروژه. |
| `app/templates/project_detail.html` | transcript محدود، فایل، milestone، assignment و status. |
| `app/templates/inbox.html` | پیام‌های داخلی/اعلان مدیر. |
| `app/templates/ticket_detail.html` | تیکت پشتیبانی. |
| `app/templates/team.html` | ساخت حساب با username/password به‌جای invite لینک‌دار. |
| `app/templates/profile_inline.html` | پروفایل و تغییر رمز. |
| `app/templates/table.html` | جدول عمومی. |
| `app/templates/categories.html` | تنظیم دسته‌بندی. |
| `app/templates/packages.html` | بسته اعتبار. |
| `app/templates/services.html` | خدمات آماده. |
| `app/templates/payments.html` | فاکتور و رسید. |
| `app/templates/wallet.html` | دفتر اعتبار. |
| `app/templates/settings.html` | SLA، تنظیمات مالی، notification template و اولویت. |

## مستندات قبلی

| فایل | موضوع و نکته |
|---|---|
| `docs/ADMIN_GUIDE_FA.md` | دسترسی پنل و نقش‌ها، براساس ساختار قبلی. |
| `docs/ARCHITECTURE.md` | توصیف API، bot، worker، DB و Docker. |
| `docs/BOT_FLOWS.md` | سناریوهای customer/admin و پرداخت. |
| `docs/DATA_MODEL.md` | entityها و روابط DB قبلی. |
| `docs/DEPLOYMENT_FA.md` | استقرار Compose و TLS. |
| `docs/DOCKER_DEPENDENCY_FIX_FA.md` | workaround build وابستگی Docker. |
| `docs/PAYMENT_SETUP.md` | Telegram Stars و Bale Wallet. |
| `docs/RUNFLARE_DNS_FIX_FA.md` | DNS/mirror وابستگی. |
| `docs/SECURITY.md` | هدرها، cookie، CSRF، backup و محدودیت‌های نگهداری. |
| `docs/SHAMSI_AND_CLEAN_CHAT_FA.md` | تاریخ جلالی و تلاش حذف پیام بعد از ذخیره. |
| `docs/SIMPLE_WIZARD_REFERRAL_FA.md` | فرم ساده بدون انتخاب دسته و referral. |
| `docs/START_MESSAGE_FIX_FA.md` | تفاوت `/start` با edit UI و لاگ Bale. |
| `docs/TEST_REPORT.md` | نتیجهٔ تست‌های قدیمی؛ ۳۰ تست اجراشده در snapshot قبلی، نه تأیید همهٔ تغییرات بستهٔ فعلی. |

## تست‌های قبلی

| فایل | دامنهٔ آزمون تعریف‌شده |
|---|---|
| `tests/conftest.py` | fixture دیتابیس/session. |
| `tests/fakes.py` | fake transport/provider. |
| `tests/contract/test_bale_adapter.py` | شکل درخواست Bale، payment token، حذف پیام و جلوگیری از چاپ URL دارای token. |
| `tests/e2e/test_project_intake.py` | wizard ساده و ادامهٔ state پس از restart. |
| `tests/integration/test_admin_auth.py` | CSRF/session login و adjustment wallet. |
| `tests/integration/test_bot_flow.py` | Inline flow، privacy، پرداخت، فایل، referral، date، edit/delete و state. |
| `tests/integration/test_http_contract.py` | health، login page و security headers. |
| `tests/integration/test_simplified_referrals.py` | ثبت referral، deep link، لغو/بازگشت و حذف picker. |
| `tests/unit/test_files_and_permissions.py` | MIME/name فایل و مجوز role. |
| `tests/unit/test_jalali.py` | ورودی، leap-year، timezone، نمایش و deadline جلالی. |
| `tests/unit/test_payments_and_identity.py` | پرداخت، account linking، code attempt و expiry. |
| `tests/unit/test_projects_wallet.py` | idempotency پروژه، wallet، priority، quote و payment. |
| `tests/unit/test_time_and_rewards.py` | SLA و referral reward cap. |
| `tests/unit/test_worker.py` | backoff و Retry-After. |

## فایل‌های تولیدشده و موقت داخل ZIP

- `.pytest_cache`-مانند پوشهٔ `pytest-of-root` شامل lockfileها و فایل‌های تصادفی خروجی تست برای نام‌های `test_file_store_sniffs_content0`، `test_manual_payment_receipt_is0` و `test_needs_info_accepts_a_priv0` بود. این فایل‌ها سورس یا دادهٔ مشتری نیستند و باید حذف شوند.
- ZIP مخزن `.git` و GitHub workflow نداشت؛ بنابراین provenance commit یا CI از خود بسته قابل بررسی نبود.
- `.env` و `projecthub.db` تنها فایل‌های حاوی تنظیم/دادهٔ محلی بودند. دیتابیس بررسی‌شده دادهٔ واقعی مشتری نداشت؛ هیچ محتوای secret از `.env` در گزارش درج نشده است.

## ارجاع‌های فنی تحقیق

انتخاب‌های بازطراحی با مستندات رسمی [Telegram Bot API](https://core.telegram.org/bots/api)، [Telegram FAQ](https://core.telegram.org/bots/faq)، [Bale Bot API](https://docs.bale.ai/)، [SQLAlchemy دربارهٔ `AsyncSession`](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)، [PostgreSQL locking clause](https://www.postgresql.org/docs/current/sql-select.html)، [Alembic cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html)، [OWASP File Upload](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)، [OWASP CSRF](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html) و [WCAG contrast](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html) تطبیق داده شده‌اند. برای کنترل متقاطع جامعه، [پرسش Stack Overflow دربارهٔ اجرای هم‌زمان query با یک AsyncSession](https://stackoverflow.com/questions/79327488/concurrent-queries-in-one-sqlalchemy-session) هم بررسی شد؛ تصمیم نهایی session-per-task بر مستند رسمی SQLAlchemy متکی است.
