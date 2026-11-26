const ORDER = /^\/orders\/[a-f0-9-]{36}$/;
const ORDER_ACTION = /^\/orders\/[a-f0-9-]{36}\/(pay|verify)$/;
const DOWNLOAD = /^\/orders\/[a-f0-9-]{36}\/download$/;
const ASSETS = new Set(["/static/checkout.css", "/static/checkout.js"]);
const START_PAY = /^\/pg\/StartPay\/A[A-Za-z0-9]{35}$/;

function allowed(method, path) {
  if (method === "POST") return path === "/orders/access" || ORDER_ACTION.test(path);
  return method === "GET" && (path === "/orders" || path === "/orders/return" ||
    ORDER.test(path) || DOWNLOAD.test(path) || ASSETS.has(path));
}

function response(message, status) {
  return new Response(message, {
    status,
    headers: { "Cache-Control": "private, no-store", "Content-Type": "text/plain; charset=utf-8" },
  });
}

function originUrl(value, publicHost) {
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && url.hostname !== publicHost &&
        /^(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]*$/i.test(url.hostname) &&
        !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash) {
      return url.origin;
    }
  } catch {
    return null;
  }
  return null;
}

function redirectLocation(location, upstreamOrigin) {
  try {
    const url = new URL(location, upstreamOrigin);
    if (url.origin === upstreamOrigin && (url.pathname === "/orders" || ORDER.test(url.pathname))) {
      return url.pathname + url.search;
    }
    if (url.origin === "https://payment.zarinpal.com" && START_PAY.test(url.pathname) && !url.search && !url.hash) {
      return url.href;
    }
  } catch {
    return null;
  }
  return null;
}

export default {
  async fetch(request, env) {
    const current = new URL(request.url);
    if (request.method === "GET" && current.pathname === "/") {
      return new Response(null, { status: 303, headers: { Location: "/orders", "Cache-Control": "no-store" } });
    }
    if (!allowed(request.method, current.pathname)) return response("یافت نشد.", 404);

    const upstreamOrigin = originUrl(env.CHECKOUT_ORIGIN, current.hostname);
    if (!upstreamOrigin) return response("مبدأ صفحهٔ سفارش هنوز تنظیم نشده است.", 503);
    if (request.method === "POST" && request.headers.get("Origin") !== current.origin) {
      return response("درخواست مجاز نیست.", 403);
    }

    const headers = new Headers();
    for (const name of ["Accept", "Content-Type", "Origin", "Range"]) {
      if (request.headers.has(name)) headers.set(name, request.headers.get(name));
    }
    const cookie = (request.headers.get("Cookie") || "").split(";")
      .map((part) => part.trim()).find((part) => part.startsWith("chito_order="));
    if (cookie) headers.set("Cookie", cookie);

    let upstream;
    try {
      upstream = await fetch(upstreamOrigin + current.pathname + current.search, {
        method: request.method,
        headers,
        body: request.method === "POST" ? request.body : null,
        redirect: "manual",
        cache: "no-store",
      });
    } catch {
      return response("اتصال به سرور سفارش برقرار نشد.", 502);
    }

    const outgoing = new Headers(upstream.headers);
    outgoing.set("Cache-Control", "private, no-store");
    if (outgoing.has("Location")) {
      const location = redirectLocation(outgoing.get("Location"), upstreamOrigin);
      if (!location) return response("آدرس بازگشت سرور سفارش معتبر نیست.", 502);
      outgoing.set("Location", location);
    }
    return new Response(upstream.body, { status: upstream.status, headers: outgoing });
  },
};
