import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "./worker.mjs";

const PUBLIC = "https://chitoproject-checkout.example.workers.dev";
const ORIGIN = "https://checkout-origin.radezaman.ir:9443";
const env = { CHECKOUT_ORIGIN: ORIGIN };
const stage = "/orders/12345678-1234-1234-1234-123456789abc";
const request = (path, init) => new Request(PUBLIC + path, init);

test("the proxy serves only checkout routes and requires HTTPS DNS origin", async () => {
  assert.equal((await worker.fetch(request("/"), env)).headers.get("Location"), "/orders");
  assert.equal((await worker.fetch(request("/admin"), env)).status, 404);
  assert.equal((await worker.fetch(request("/static/admin.js"), env)).status, 404);
  assert.equal((await worker.fetch(request("/orders%2Freturn"), env)).status, 404);
  assert.equal((await worker.fetch(request("/orders"), {})).status, 503);
  assert.equal((await worker.fetch(request("/orders"), { CHECKOUT_ORIGIN: "https://46.249.107.214:9443" })).status, 503);
  assert.equal((await worker.fetch(request("/orders/access", { method: "POST" }), env)).status, 403);
});

test("the proxy preserves checkout form origin, body, session and redirect", async (t) => {
  t.mock.method(globalThis, "fetch", async (url, init) => {
    assert.equal(url, ORIGIN + "/orders/access");
    assert.equal(init.headers.get("Origin"), PUBLIC);
    assert.equal(init.headers.get("Cookie"), "chito_order=customer-session");
    assert.equal(await new Response(init.body).text(), "token=sample");
    assert.equal(init.redirect, "manual");
    assert.equal(init.cache, "no-store");
    return new Response(null, {
      status: 303,
      headers: {
        Location: ORIGIN + stage,
        "Set-Cookie": "chito_order=sample; Secure; HttpOnly; SameSite=Lax",
      },
    });
  });
  const result = await worker.fetch(request("/orders/access", {
    method: "POST",
    headers: { Origin: PUBLIC, Cookie: "admin=private; chito_order=customer-session" },
    body: "token=sample",
  }), env);
  assert.equal(result.status, 303);
  assert.equal(result.headers.get("Location"), stage);
  assert.match(result.headers.get("Set-Cookie"), /HttpOnly/);
  assert.equal(result.headers.get("Cache-Control"), "private, no-store");
});

test("the proxy preserves only the expected gateway redirect and forwards callback parameters", async (t) => {
  const authority = "A" + "0".repeat(35);
  const location = "https://payment.zarinpal.com/pg/StartPay/" + authority;
  const callback = "/orders/return?Authority=" + authority + "&Status=OK";
  t.mock.method(globalThis, "fetch", async (url) => {
    if (url === ORIGIN + stage + "/pay") return new Response(null, { status: 303, headers: { Location: location } });
    assert.equal(url, ORIGIN + callback);
    return new Response(null, { status: 303, headers: { Location: stage + "?result=verified" } });
  });
  const pay = await worker.fetch(request(stage + "/pay", { method: "POST", headers: { Origin: PUBLIC } }), env);
  assert.equal(pay.headers.get("Location"), location);
  const returned = await worker.fetch(request(callback), env);
  assert.equal(returned.headers.get("Location"), stage + "?result=verified");
});

test("downloads stream without caching and unrelated redirects are rejected", async (t) => {
  t.mock.method(globalThis, "fetch", async (url, init) => {
    if (url === ORIGIN + stage + "/download") {
      assert.equal(init.headers.get("Range"), "bytes=0-2");
      return new Response("zip", { status: 206, headers: { "Content-Disposition": "attachment; filename=file.zip" } });
    }
    return new Response(null, { status: 303, headers: { Location: "https://example.com/" } });
  });
  const result = await worker.fetch(request(stage + "/download", { headers: { Range: "bytes=0-2" } }), env);
  assert.equal(result.status, 206);
  assert.equal(await result.text(), "zip");
  assert.match(result.headers.get("Content-Disposition"), /attachment/);
  assert.equal((await worker.fetch(request("/orders"), env)).status, 502);
});
