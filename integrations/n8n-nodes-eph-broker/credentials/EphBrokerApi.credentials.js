'use strict';

// One credential for both nodes. The trigger uses eventsSecret to check X-Eph-Signature; the action node uses
// baseUrl and the optional proxy header, since /runs and /health have no login of their own.
class EphBrokerApi {
  constructor() {
    this.name = 'ephBrokerApi';
    this.displayName = 'eph-broker API';
    this.documentationUrl = 'https://github.com/mdbook/eph-broker/blob/main/docs/n8n.md';
    this.properties = [
      {
        displayName: 'Base URL',
        name: 'baseUrl',
        type: 'string',
        default: '',
        placeholder: 'https://broker.example.com',
        description: 'Where n8n reaches the broker, usually a reverse proxy in front of 127.0.0.1:8787',
      },
      {
        displayName: 'Events Secret',
        name: 'eventsSecret',
        type: 'string',
        typeOptions: { password: true },
        default: '',
        description: "Same value as the broker's EVENTS_WEBHOOK_SECRET. The trigger refuses every event while it's empty.",
      },
      {
        displayName: 'Proxy Header Name',
        name: 'headerName',
        type: 'string',
        default: '',
        placeholder: 'Authorization',
        description: 'Optional header your authenticating proxy wants on /runs and /health',
      },
      {
        displayName: 'Proxy Header Value',
        name: 'headerValue',
        type: 'string',
        typeOptions: { password: true },
        default: '',
      },
    ];
  }
}

module.exports = { EphBrokerApi };
