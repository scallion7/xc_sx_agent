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
      error.code = data.error_code;
      throw error;
    }
    return data;
  }

  async pending(path, scope, token = this.token()) {
    const slot = await this.scopeKey(`pending:${scope}:${path}`, token);
    return JSON.parse(sessionStorage.getItem(slot) || 'null');
  }

  async retryPending(path, scope, token = this.token()) {
    const pending = await this.pending(path, scope, token);
    if (!pending) throw new Error('没有待核对的提交，请刷新处理进度。');
    // Replay the saved revision and payload, even after a page reload has read
    // newer state. The server checks the receipt before its revision fence.
    return this.write(path, JSON.parse(pending.payload), scope, token);
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
        // This specific CAS rejection guarantees no command was committed.
        // Other conflicts, transport failures and 5xx keep the original intent.
        if ([400, 401, 403, 404, 405, 413, 422].includes(error.status) ||
            (error.status === 409 && error.code === 'INTAKE_VERSION_CONFLICT')) {
          sessionStorage.removeItem(slot);
        }
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
