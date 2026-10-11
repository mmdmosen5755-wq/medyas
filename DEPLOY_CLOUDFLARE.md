# اجرای medyas روی Cloudflare (بدون سرور)

معماری: یک Worker کوچک (`src/index.ts`) ← یک کانتینر Docker با برنامه‌ی پایتون ← دیتابیس Postgres بیرونی.
Cron هر دقیقه کانتینر را بیدار نگه می‌دارد تا ربات تلگرام همیشه در حال دریافت پیام باشد.

## پیش‌نیازها
- اکانت Cloudflare با پلن **Workers Paid (۵ دلار در ماه)**؛ Containers بدون آن کار نمی‌کند و نیاز به کارت بین‌المللی دارد.
- یک ریپوی GitHub (همین پروژه).
- یک Postgres رایگان: در neon.com پروژه بسازید و «Connection string» را کپی کنید (شبیه `postgresql://user:pass@ep-xxx-pooler.../db?sslmode=require`).
  کد برای این فرمت آماده است؛ چیزی را دستی تغییر ندهید.

## مراحل
1. **توکن Cloudflare:** My Profile ← API Tokens ← Create Token ← قالب «Edit Cloudflare Workers». اگر موقع دیپلوی خطای دسترسی گرفتید، همان مجوزی را که خطا نام می‌برد اضافه کنید.
   همچنین Account ID را از صفحه‌ی Workers & Pages کپی کنید.
2. **GitHub:** Settings ← Secrets and variables ← Actions ← دو Secret بسازید: `CLOUDFLARE_API_TOKEN` و `CLOUDFLARE_ACCOUNT_ID`.
3. کد را در شاخه‌ی `main` پوش کنید (`.env` را **هرگز** پوش نکنید). Actions خودکار دیپلوی می‌کند (اولین بار چند دقیقه طول می‌کشد).
4. **متغیرهای برنامه:** Cloudflare Dashboard ← Workers & Pages ← medyas ← Settings ← Variables and Secrets ← (نوع Secret):
   - `DATABASE_URL` = رشته‌ی Neon
   - `SECRET_KEY` = رشته‌ی تصادفی طولانی (اگر از دیتابیس قبلی بکاپ دارید همان قبلی، وگرنه جدید)
   - `ADMIN_PASSWORD` = رمز پنل
   - اختیاری: `ANTHROPIC_API_KEY`، `AI_PROXY`، `EITAA_PROXY`، `EITAA_WEB_BASE`
5. در GitHub ← Actions ← Deploy to Cloudflare ← Run workflow (برای اینکه کانتینر با متغیرهای جدید بالا بیاید).
6. آدرس Worker (مثل `https://medyas.<حساب‌شما>.workers.dev`) را باز کنید: `/health` باید `ok` بدهد؛ سپس `/login`.
7. در پنل ← تنظیمات: توکن ربات تلگرام و آیدی ادمین را بگذارید (مثل قبل).

## نکته‌ها
- فقط `max_instances = 1` است و نباید زیاد شود (ربات با long-polling کار می‌کند).
- دیسک کانتینر موقتی است؛ همه‌چیز در Postgres می‌ماند. ری‌استارت‌ها خودکار بازیابی می‌شوند (پست‌های نیمه‌کاره دوباره در صف می‌روند).
- آدرس `workers.dev` ممکن است از ایران فیلتر باشد؛ فقط ادمین به پنل نیاز دارد. می‌توانید دامنه‌ی خودتان را در Cloudflare به Worker وصل کنید.
- هزینه (تخمین از نرخ‌های منتشرشده، کانتینر ۲۴ ساعته با ۱ گیگ حافظه): حدود ۵ دلار پلن + حدود ۷ دلار مصرف ≈ ۱۲ دلار در ماه. حافظه بر اساس مقدار رزروشده حساب می‌شود، نه مصرف واقعی.
- لاگ‌ها: Dashboard ← medyas ← Observability (یا `npx wrangler tail`).
- اجرای محلی دیپلوی (اگر Docker دارید): `npm install && npx wrangler deploy`.
