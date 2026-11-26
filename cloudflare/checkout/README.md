# صفحهٔ سفارش ChitoProject روی Cloudflare Workers

Worker مسیرهای پرداخت و دانلود را به نصب ChitoProject می‌رساند. Python، پایگاه داده، مرچنت زرین‌پال و فایل‌های مشتری روی VPS می‌مانند.

## اتصال GitHub

در Workers & Pages گزینهٔ ساخت Worker و اتصال GitHub را انتخاب کنید. دسترسی GitHub App را به مخزن این پروژه محدود کنید.

| گزینه | مقدار |
| --- | --- |
| Repository | `Alidl81/chitoproject` |
| Production branch | `main` |
| Worker name | `chitoproject-checkout` |
| Root directory | `cloudflare/checkout` |
| Build command | خالی |
| Deploy command | `npx wrangler deploy` |

پس از Deploy، نشانی `workers.dev` را از Domains & Routes کپی کنید. تا زمانی که مبدأ تنظیم نشده، `/orders` کد ۵۰۳ می‌دهد.

## مبدأ HTTPS

برای اتصال مستقیم Worker به VPS، در ارائه‌دهندهٔ فعلی DNS یک رکورد A از نوع DNS only بسازید، مثلاً `checkout-origin.radezaman.ir → 46.249.107.214`. رکوردهای فعلی وردپرس را تغییر ندهید.

روی VPS، این بلوک را در فایلی جداگانه در مسیر import فعلی Caddy قرار دهید. این بلوک روی پورت ۹۴۴۳ فقط مسیرهای سفارش را به برنامه منتقل می‌کند. پورت ۸۰ برای HTTP-01 باید قابل دسترس باشد. مقدار Host در reverse proxy با میزبان فعلی مجاز برنامه مطابقت دارد.

```caddyfile
https://checkout-origin.radezaman.ir:9443 {
    tls {
        issuer acme {
            disable_tlsalpn_challenge
        }
    }
    @checkout path /orders /orders/* /static/checkout.css /static/checkout.js
    handle @checkout {
        reverse_proxy 127.0.0.1:8000 {
            header_up Host 46.249.107.214
        }
    }
    handle {
        respond 404
    }
}
```

ابتدا `sudo caddy validate --config /etc/caddy/Caddyfile` و پس از موفقیت `sudo systemctl reload caddy` را اجرا کنید. پس از نصب نسخهٔ پرداخت، با curl عادی روی مبدأ بررسی کنید `/orders` کد ۲۰۰ و `/admin` کد ۴۰۴ بدهند.

## اتصال صفحه و درگاه

1. در Settings → Variables and Secrets در Worker، یک متغیر Text به نام `CHECKOUT_ORIGIN` و با مقدار `https://checkout-origin.radezaman.ir:9443` اضافه و منتشر کنید.
2. در فایل خصوصی `/etc/chitoproject/chitoproject.env` روی VPS، `CHECKOUT_BASE_URL` را برابر نشانی کامل HTTPS Worker بدون مسیر قرار دهید. برای بسته‌های رمزدار، `PAYMENT_ARCHIVE_KEY` پایدار لازم است. `PUBLIC_BASE_URL` نشانی فعلی پنل است.
3. نسخهٔ فعلی `main` را روی VPS مستقر کنید. `/orders` و `/static/checkout.css` روی Worker باید ۲۰۰ و `/admin` باید ۴۰۴ بدهند.
4. پیش از وارد کردن `ZARINPAL_MERCHANT_ID`، از زرین‌پال تأیید بگیرید که نشانی Worker با مسیر `/orders/return` و IP خروجی VPS را می‌پذیرد. درخواست‌های ایجاد و تأیید پرداخت از VPS می‌روند. اگر `workers.dev` پذیرفته نشد، همین Worker را به دامنهٔ اختصاصی تأییدشده متصل کنید و `CHECKOUT_BASE_URL` را تغییر دهید.

آزمایش محلی Worker:

```sh
node --test cloudflare/checkout/worker.test.mjs
```
