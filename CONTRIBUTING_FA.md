# مشارکت در ChitoProject

## پیش از تغییر

1. Issue را با رفتار مورد انتظار و معیار پذیرش باز کنید.
2. تغییر را کوچک نگه دارید و یک مسئول بررسی مشخص کنید.
3. اگر مدل داده تغییر می‌کند، migration تازه بسازید؛ migration اولیه را پس از انتشار ویرایش نکنید.
4. هیچ token، `.env`، دیتابیس محلی، transcript مشتری یا فایل upload را commit نکنید.

## کیفیت تغییر

- منطق workflow را در `app/bot/flow.py`، لایهٔ provider را در `app/bot/transport.py` و routeهای مدیریت را در `app/web/` نگه دارید.
- برای هر ورودی کاربر طول و نوع را محدود کنید؛ متن و نام فایل را در HTML با Jinja escaping نمایش دهید.
- مجوز را در سرور بررسی کنید؛ پنهان کردن دکمه کافی نیست.
- پیام‌رسان و دیتابیس را در transaction مختصر نگه دارید و ارسال خارجی را از outbox انجام دهید.
- retry باید idempotent یا دارای کلید dedupe باشد.
- رابط باید RTL، keyboard-accessible، responsive و در هر دو theme قابل‌خواندن باشد.

## پیش از merge

```bash
ruff check app tests
pytest -q
```

برای migration، PostgreSQL و backup/restore نیز در محیط آماده‌سازی بررسی انجام دهید. CI روی pull request این دو بررسی پایه را اجرا می‌کند؛ بررسی توکن واقعی و شبکه در production انجام نمی‌شود.
