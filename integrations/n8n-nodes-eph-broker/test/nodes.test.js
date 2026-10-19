'use strict';
// node --test test/  (no n8n needed: each node method runs against a small fake of n8n's context)

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const ROOT = path.join(__dirname, '..');
const { EphBrokerTrigger, verifySignature, seenIds } = require('../nodes/EphBrokerTrigger.node.js');
const { EphBroker } = require('../nodes/EphBroker.node.js');

const SECRET = 'test-events-secret';
const sign = (raw, secret = SECRET) => 'sha256=' + crypto.createHmac('sha256', secret).update(raw).digest('hex');
const now = () => Math.floor(Date.now() / 1000);

let wf = 0; // a fresh workflow id per test case unless one is passed, so dedupe memory doesn't leak between cases
function delivery(body, { sig, params = {}, workflowId = `wf${++wf}`, secret = SECRET } = {}) {
  const raw = Buffer.from(typeof body === 'string' ? body : JSON.stringify(body));
  const res = { code: null, body: null };
  const ctx = {
    getRequestObject: () => ({ headers: { 'x-eph-signature': sig === undefined ? sign(raw) : sig }, rawBody: raw }),
    getResponseObject: () => ({ status(c) { res.code = c; return this; }, json(b) { res.body = b; } }),
    getCredentials: async () => ({ eventsSecret: secret }),
    getNodeParameter: (name, dflt) => (name in params ? params[name] : dflt),
    getWorkflow: () => ({ id: workflowId }),
    getNode: () => ({ name: 'eph-broker Trigger' }),
  };
  return { ctx, res };
}

const event = (over = {}) => ({ v: 1, id: 'd1', event: 'run.finished', ts: now(), subject_key: 'vikunja:task:9001', outcome: 'closed', ...over });

test('a signed, fresh event starts the workflow with the parsed body', async () => {
  const d = delivery(event());
  const r = await new EphBrokerTrigger().webhook.call(d.ctx);
  assert.deepEqual(r.workflowData[0][0].json, event({ ts: r.workflowData[0][0].json.ts }));
  assert.deepEqual(r.webhookResponse, { ok: true });
  assert.equal(d.res.code, null);
});

test('a wrong, missing or other-secret signature gets a 401 and no run', async () => {
  for (const opts of [{ sig: 'sha256=00' }, { sig: '' }, { sig: sign(Buffer.from('{}')) }, { secret: 'other' }]) {
    const d = delivery(event(), opts);
    const r = await new EphBrokerTrigger().webhook.call(d.ctx);
    assert.deepEqual(r, { noWebhookResponse: true });
    assert.equal(d.res.code, 401);
  }
});

test('an empty secret refuses everything, even a body signed with the empty key', async () => {
  const raw = Buffer.from(JSON.stringify(event()));
  assert.equal(verifySignature('', raw, sign(raw, '')), false);
  const d = delivery(event(), { secret: '', sig: sign(raw, '') });
  assert.deepEqual(await new EphBrokerTrigger().webhook.call(d.ctx), { noWebhookResponse: true });
});

test('a stale event is refused, unless max age is 0', async () => {
  const old = event({ ts: now() - 3600 });
  let d = delivery(old);
  assert.deepEqual(await new EphBrokerTrigger().webhook.call(d.ctx), { noWebhookResponse: true });
  assert.equal(d.res.code, 401);
  d = delivery(old, { params: { maxAge: 0 } });
  assert.ok((await new EphBrokerTrigger().webhook.call(d.ctx)).workflowData);
});

test('a NaN or negative max age falls back to the default instead of turning the check off', async () => {
  const old = event({ ts: now() - 3600 });
  for (const maxAge of [NaN, 'abc', -1, -3600, Infinity]) {
    const d = delivery(old, { params: { maxAge } });
    assert.deepEqual(await new EphBrokerTrigger().webhook.call(d.ctx), { noWebhookResponse: true }, String(maxAge));
    assert.equal(d.res.code, 401);
  }
  const d = delivery(event({ ts: now() - 60 }), { params: { maxAge: -1 } });
  assert.ok((await new EphBrokerTrigger().webhook.call(d.ctx)).workflowData);
});

test('a signed body that is not an event JSON gets a 400', async () => {
  for (const body of ['not json', '[]', '{"ts":1}']) {
    const d = delivery(body);
    assert.deepEqual(await new EphBrokerTrigger().webhook.call(d.ctx), { noWebhookResponse: true });
    assert.equal(d.res.code, 400);
  }
});

test('the events filter answers 200 without a run for unselected events', async () => {
  const d = delivery(event({ event: 'run.started' }), { params: { events: ['mr.opened'] } });
  const r = await new EphBrokerTrigger().webhook.call(d.ctx);
  assert.equal(r.workflowData, undefined);
  assert.equal(r.webhookResponse.ignored, 'event not selected');
  const d2 = delivery(event({ event: 'mr.opened' }), { params: { events: ['mr.opened'] } });
  assert.ok((await new EphBrokerTrigger().webhook.call(d2.ctx)).workflowData);
});

test('a redelivery with the same id fires once, and the id list stays bounded', async () => {
  const workflowId = 'dedupe';
  const first = await new EphBrokerTrigger().webhook.call(delivery(event(), { workflowId }).ctx);
  const again = await new EphBrokerTrigger().webhook.call(delivery(event(), { workflowId }).ctx);
  const elsewhere = await new EphBrokerTrigger().webhook.call(delivery(event(), { workflowId: 'other' }).ctx);
  assert.ok(first.workflowData);
  assert.equal(again.workflowData, undefined);
  assert.equal(again.webhookResponse.ignored, 'duplicate delivery');
  assert.ok(elsewhere.workflowData, 'another workflow keeps its own list');
  for (let i = 0; i < 600; i++) await new EphBrokerTrigger().webhook.call(delivery(event({ id: `x${i}` }), { workflowId }).ctx);
  assert.equal(seenIds.get('dedupe/eph-broker Trigger').length, 500);
});

function action(params, responder, { creds = {}, failOk = false } = {}) {
  const calls = [];
  const ctx = {
    getInputData: () => params.map(() => ({ json: {} })),
    getCredentials: async () => ({ baseUrl: 'https://broker.example.com/', ...creds }),
    getNodeParameter: (name, i) => params[i][name],
    continueOnFail: () => failOk,
    helpers: { httpRequest: async (opts) => { calls.push(opts); return responder(opts); } },
  };
  return { ctx, calls };
}

test('Get Run Status escapes the key, and a 404 comes back as the broker answer', async () => {
  const body = { ok: false, subject_key: 'github:acme/app#9001', status: 'unknown' };
  const a = action([{ operation: 'getRun', subjectKey: 'github:acme/app#9001' }], () => ({ statusCode: 404, body }));
  const [out] = await new EphBroker().execute.call(a.ctx);
  assert.equal(a.calls[0].url, 'https://broker.example.com/runs/github%3Aacme%2Fapp%239001');
  assert.equal(a.calls[0].method, 'GET');
  assert.deepEqual(out[0].json, body);
});

test('Get Health sends the proxy header and returns the body', async () => {
  const a = action([{ operation: 'getHealth' }], () => ({ statusCode: 200, body: { ok: true } }),
    { creds: { headerName: 'Authorization', headerValue: 'Bearer t' } });
  const [out] = await new EphBroker().execute.call(a.ctx);
  assert.equal(a.calls[0].url, 'https://broker.example.com/health');
  assert.equal(a.calls[0].headers.Authorization, 'Bearer t');
  assert.deepEqual(out[0].json, { ok: true });
});

test('any other status fails the node, or becomes an error item with continue on fail', async () => {
  const p = [{ operation: 'getRun', subjectKey: 'vikunja:task:9001' }];
  await assert.rejects(new EphBroker().execute.call(action(p, () => ({ statusCode: 502, body: '' })).ctx), /HTTP 502/);
  await assert.rejects(new EphBroker().execute.call(action([{ operation: 'getHealth' }], () => ({ statusCode: 404 })).ctx), /HTTP 404/);
  const [out] = await new EphBroker().execute.call(action(p, () => ({ statusCode: 401 }), { failOk: true }).ctx);
  assert.match(out[0].json.error, /HTTP 401/);
});

test('package.json lists every node and credential the way n8n loads them', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
  assert.ok(pkg.name.startsWith('n8n-nodes-'));
  assert.ok(pkg.keywords.includes('n8n-community-node-package'));
  const files = [...pkg.n8n.nodes, ...pkg.n8n.credentials];
  assert.equal(files.length, 3);
  for (const f of files) {
    // n8n takes the class name from the file name (EphBroker.node.js -> EphBroker)
    const cls = path.basename(f).split('.')[0];
    const Mod = require(path.join(ROOT, f))[cls];
    assert.equal(typeof Mod, 'function', f);
    const inst = new Mod();
    const icon = inst.description && inst.description.icon;
    if (icon) assert.ok(fs.existsSync(path.join(ROOT, path.dirname(f), icon.replace('file:', ''))), icon);
  }
  const creds = new (require(path.join(ROOT, pkg.n8n.credentials[0])).EphBrokerApi)();
  for (const f of pkg.n8n.nodes) {
    const cls = path.basename(f).split('.')[0];
    const d = new (require(path.join(ROOT, f))[cls])().description;
    assert.deepEqual(d.credentials.map((c) => c.name), [creds.name]);
  }
});
