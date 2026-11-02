// A watcher captures its run and identity; switching sessions cancels old work.
export class RunStream {
  constructor() {
    this.controller = null;
  }
  stop() {
    if (this.controller) this.controller.abort();
  }
  async watch(runId, token, cursorKey, onEvent) {
    this.stop();
    const controller = new AbortController();
    this.controller = controller;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const response = await fetch(`/v1/runs/${encodeURIComponent(runId)}/events`, {
          headers: {
            'X-API-Key': token,
            'Last-Event-ID': sessionStorage.getItem(cursorKey) || '0'
          },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error('事件连接失败，请重新连接');
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          const part = await reader.read();
          if (part.done) break;
          buffer += decoder.decode(part.value, {
            stream: true
          }).replace(/\r\n/g, '\n');
          let boundary;
          while ((boundary = buffer.indexOf('\n\n')) >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const id = block.match(/^id: (\d+)/m),
              data = block.match(/^data: (.*)$/m);
            if (id && data) {
              const event = JSON.parse(data[1]);
              onEvent(event);
              sessionStorage.setItem(cursorKey, id[1]);
            }
          }
        }
        return !controller.signal.aborted;
      } catch (error) {
        if (controller.signal.aborted) return false;
        if (attempt === 2) throw error;
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    }
  }
}
