const probe = async () => {
  const call = async (method, path, body, token) => {
    const url = `http://localhost:8080${path}`;
    const headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    try {
      const r = await fetch(url, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(6000),
      });
      const text = await r.text();
      return { status: r.status, body: text.slice(0, 300) };
    } catch (e) {
      return { status: null, body: `API_NOT_REACHABLE: ${e.message}` };
    }
  };

  console.log("=== login (correct) ===");
  const l1 = await call("POST", "/api/auth/login", { email: "analyst@waspada.demo", password: "waspada123" });
  console.log(l1.status, l1.body);

  console.log("=== login (wrong) ===");
  const l2 = await call("POST", "/api/auth/login", { email: "analyst@waspada.demo", password: "wrongpass1" });
  console.log(l2.status, l2.body);

  let token = null;
  if (l1.status === 200) {
    try { token = JSON.parse(l1.body).token; } catch {}
  }
  if (token) {
    console.log("=== /api/auth/me ===");
    const me = await call("GET", "/api/auth/me", null, token);
    console.log(me.status, me.body);
    console.log("=== /api/run without token ===");
    const r3 = await call("POST", "/api/run?brain=mock");
    console.log(r3.status, r3.body);
    console.log("=== /api/run with token ===");
    const r4 = await call("POST", "/api/run?brain=mock", null, token);
    console.log(r4.status, r4.body.slice(0, 150));
  }
};
probe();
