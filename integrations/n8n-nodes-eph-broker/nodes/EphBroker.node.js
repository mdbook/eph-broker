'use strict';

// Read-only on purpose: the broker has no way to start a worker over HTTP from outside a tracker, and this node
// doesn't add one. To give the broker work, create or label a ticket in your tracker.
class EphBroker {
  constructor() {
    this.description = {
      displayName: 'eph-broker',
      name: 'ephBroker',
      icon: 'file:ephBroker.svg',
      group: ['transform'],
      version: 1,
      subtitle: '={{$parameter["operation"]}}',
      description: 'Look up a run or the health of an eph-broker',
      defaults: { name: 'eph-broker' },
      inputs: ['main'],
      outputs: ['main'],
      credentials: [{ name: 'ephBrokerApi', required: true }],
      properties: [
        {
          displayName: 'Operation',
          name: 'operation',
          type: 'options',
          noDataExpression: true,
          options: [
            { name: 'Get Run Status', value: 'getRun', action: 'Get the status of a run', description: 'GET /runs/<subject_key>' },
            { name: 'Get Health', value: 'getHealth', action: 'Get broker health', description: 'GET /health' },
          ],
          default: 'getRun',
        },
        {
          displayName: 'Subject Key',
          name: 'subjectKey',
          type: 'string',
          required: true,
          default: '',
          placeholder: 'vikunja:task:42',
          description: 'The ticket or chat the run is about, as in the subject_key of a run event',
          displayOptions: { show: { operation: ['getRun'] } },
        },
      ],
    };
  }

  async execute() {
    const items = this.getInputData();
    const creds = await this.getCredentials('ephBrokerApi');
    const base = String(creds.baseUrl || '').replace(/\/+$/, '');
    const headers = { Accept: 'application/json' };
    if (creds.headerName) headers[creds.headerName] = creds.headerValue || '';
    const out = [];
    for (let i = 0; i < items.length; i++) {
      try {
        const op = this.getNodeParameter('operation', i);
        const url = op === 'getRun'
          ? `${base}/runs/${encodeURIComponent(this.getNodeParameter('subjectKey', i))}`
          : `${base}/health`;
        const res = await this.helpers.httpRequest({
          method: 'GET', url, headers, json: true, returnFullResponse: true, ignoreHttpStatusErrors: true,
        });
        // 404 on /runs is an answer (status "unknown"), not a failure
        if (res.statusCode === 200 || (op === 'getRun' && res.statusCode === 404)) {
          out.push({ json: res.body, pairedItem: { item: i } });
        } else {
          throw new Error(`eph-broker answered HTTP ${res.statusCode} for ${url}`);
        }
      } catch (e) {
        if (!this.continueOnFail()) throw e;
        out.push({ json: { error: e.message }, pairedItem: { item: i } });
      }
    }
    return [out];
  }
}

module.exports = { EphBroker };
