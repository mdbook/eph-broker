'use strict';

const crypto = require('crypto');

const EVENTS = ['run.started', 'run.finished', 'run.escalated', 'mr.opened'];
const MAX_AGE_DEFAULT = 300; // seconds
const SEEN_MAX = 500; // delivery ids remembered per trigger; the broker's retries come within 12 seconds
// Kept in memory, not in workflow static data: n8n 2.x loses static data a webhook writes once the workflow already
// has some (starting the execution resets its changed flag). So an n8n restart forgets, which retries don't outlive.
const seenIds = new Map();

// sha256=<hex HMAC-SHA256 of the raw body> (docs/events.md), compared in constant time
function verifySignature(secret, raw, header) {
  if (!secret || !Buffer.isBuffer(raw)) return false;
  const want = Buffer.from('sha256=' + crypto.createHmac('sha256', secret).update(raw).digest('hex'));
  const got = Buffer.from(String(header || ''));
  return want.length === got.length && crypto.timingSafeEqual(want, got);
}

// The checks in order, each one cheap and before the next: signature over the exact bytes, then parse, then age.
// Returns { event } or { code, error } for the HTTP answer.
function checkDelivery(secret, raw, header, maxAge, nowSec) {
  if (!Buffer.isBuffer(raw)) return { code: 500, error: 'no raw body' };
  if (!verifySignature(secret, raw, header)) return { code: 401, error: 'bad signature' };
  let event;
  try {
    event = JSON.parse(raw.toString('utf8'));
  } catch (e) {
    return { code: 400, error: 'body is not JSON' };
  }
  if (!event || typeof event !== 'object' || typeof event.event !== 'string') return { code: 400, error: 'not an event' };
  // 0 turns the age check off. Anything else that isn't a finite number >= 0 (NaN, negative) gets the default.
  const limit = Number.isFinite(maxAge) && maxAge >= 0 ? maxAge : MAX_AGE_DEFAULT;
  if (limit > 0 && !(Math.abs(nowSec - Number(event.ts)) <= limit)) return { code: 401, error: 'stale event' };
  return { event };
}

class EphBrokerTrigger {
  constructor() {
    this.description = {
      displayName: 'eph-broker Trigger',
      name: 'ephBrokerTrigger',
      icon: 'file:ephBroker.svg',
      group: ['trigger'],
      version: 1,
      subtitle: '={{$parameter["events"].join(", ") || "all events"}}',
      description: 'Starts the workflow on a signed eph-broker run event',
      defaults: { name: 'eph-broker Trigger' },
      inputs: [],
      outputs: ['main'],
      credentials: [{ name: 'ephBrokerApi', required: true }],
      webhooks: [{ name: 'default', httpMethod: 'POST', responseMode: 'onReceived', responseData: '', path: 'events' }],
      properties: [
        {
          displayName: 'Events',
          name: 'events',
          type: 'multiOptions',
          options: EVENTS.map((e) => ({ name: e, value: e })),
          default: [],
          description: 'Which events start the workflow. Empty means all of them.',
        },
        {
          displayName: 'Max Age (Seconds)',
          name: 'maxAge',
          type: 'number',
          default: MAX_AGE_DEFAULT,
          description: "Refuse events whose ts is further than this from n8n's clock. 0 turns the check off.",
        },
      ],
    };
  }

  async webhook() {
    const req = this.getRequestObject();
    const res = this.getResponseObject();
    if (!Buffer.isBuffer(req.rawBody) && typeof req.readRawBody === 'function') await req.readRawBody();
    const creds = await this.getCredentials('ephBrokerApi');
    const maxAge = Number(this.getNodeParameter('maxAge', MAX_AGE_DEFAULT));
    const got = checkDelivery(creds.eventsSecret, req.rawBody, req.headers['x-eph-signature'], maxAge, Date.now() / 1000);
    if (got.error) {
      // 4xx: the broker doesn't retry it. The reason stays generic so a prober learns nothing about the secret.
      res.status(got.code).json({ ok: false, error: got.error });
      return { noWebhookResponse: true };
    }
    const event = got.event;
    const wanted = this.getNodeParameter('events', []);
    if (wanted.length && !wanted.includes(event.event)) return { webhookResponse: { ok: true, ignored: 'event not selected' } };
    // A retry reuses the delivery id
    const where = `${this.getWorkflow().id}/${this.getNode().name}`;
    const seen = seenIds.get(where) || [];
    if (event.id && seen.includes(event.id)) return { webhookResponse: { ok: true, ignored: 'duplicate delivery' } };
    if (event.id) seenIds.set(where, seen.concat(event.id).slice(-SEEN_MAX));
    return { workflowData: [[{ json: event }]], webhookResponse: { ok: true } };
  }
}

module.exports = { EphBrokerTrigger, verifySignature, checkDelivery, seenIds };
