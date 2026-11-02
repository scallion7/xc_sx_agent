// One logical write keeps its key until a definitive response is received.
export class RequestClient {
  constructor(token) {
    this.token = token;
    this.inflight = new Map();
  }

  async scopeKey(scope, token = this.token()) {
    const bytes = new TextEncoder().encode(token);
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    const identity = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
    return `ecom:${identity}:${scope}`;
  }

  async request(path, method = 'GET', body, headers = {}, token = this.token()) {
    const response = await fetch(path, {
      method,
      headers: {
        'X-API-Key': token,
        'Content-Type': 'application/json',
        ...headers
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error((data.error_code || response.status) + ': ' + JSON.stringify(data.detail || '请求未完成'));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async write(path, body, scope, token = this.token()) {
    const slot = await this.scopeKey(`pending:${scope}:${path}`, token);
    const payload = JSON.stringify(body);
    const current = this.inflight.get(slot);
    if (current) {
      if (current.payload !== payload) throw new Error('上一条请求尚未确认，请先重试原内容。');
      return current.promise;
    }
    const pending = JSON.parse(sessionStorage.getItem(slot) || 'null');
    if (pending && pending.payload !== payload) {
      throw new Error('上一条请求结果未知，请恢复原内容并重试，或先查询处理记录。');
    }
    const intent = pending || {
      key: crypto.randomUUID(),
      payload
    };
    // Persist before fetch: refresh/network loss cannot silently create a new key.
    sessionStorage.setItem(slot, JSON.stringify(intent));
    const promise = this.request(path, 'POST', body, {
        'Idempotency-Key': intent.key
      }, token)
      .then(data => {
        sessionStorage.removeItem(slot);
        return data;
      })
      .catch(error => {
        // Validation/auth failures are definitive; transport, 5xx and conflict outcomes are not.
        if ([400, 401, 403, 404, 405, 413, 422].includes(error.status)) sessionStorage.removeItem(slot);
        if (!error.status) error.message = '连接中断，处理结果待确认。请保持原内容重试，将复用同一请求编号。';
        throw error;
      })
      .finally(() => this.inflight.delete(slot));
    this.inflight.set(slot, {
      payload,
      promise
    });
    return promise;
  }
}
