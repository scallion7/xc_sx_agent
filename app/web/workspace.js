import {
  RequestClient
} from './request_client.js';
import {
  RunStream
} from './run_stream.js';
const $ = id => document.getElementById(id);
$('token').value = sessionStorage.getItem('agentToken') || '';
$('session').value = sessionStorage.getItem('agentSession') || 'demo';
const displayedRuns = new Set();
const labels = {
  queued: '排队中',
  running: '执行中',
  completed: '已完成',
  retry_wait: '等待重试',
  cancelled: '已取消',
  dead_letter: '执行失败',
  needs_reconciliation: '结果待人工核对',
  resolved: '已核对结案',
  approved: '已批准',
  pending_review: '等待人工审批',
  pending: '待处理',
  processing: '处理中',
  succeeded: '成功',
  failed: '失败',
  shipped: '已发货',
  delivered: '已签收',
  paid: '已付款'
};
let runId = '';
const client = new RequestClient(() => $('token').value);
const stream = new RunStream();
const runSlot = () => client.scopeKey('run:' + $('session').value);
const session = () => encodeURIComponent($('session').value);

function status(text, error = false) {
  $('status').textContent = text;
  $('status').classList.toggle('error', error)
}
const api = (...args) => client.request(...args);

function show(value, target) {
  const order = value.order,
    operation = value.operation;
  if (order) {
    $(target).textContent = '订单 ' + order.order_id + '\n状态：' + (labels[order.status] || order.status) + '\n金额：¥ ' + order.total + '\n' + (order.items || []).map(item => item.name + ' × ' + item.quantity).join('\n')
  } else if (operation) {
    $(target).textContent = '退款状态：' + (labels[operation.status] || operation.status) + '\n' + (value.message || '') + '\n处理编号：' + operation.operation_id
  } else $(target).textContent = value.message || value.error || '尚无处理结果';
}

function bubble(text) {
  if (!text) return;
  const node = document.createElement('div');
  node.className = 'bubble';
  node.textContent = text;
  $('conversation').append(node)
}

function handle(fn) {
  return async () => {
    try {
      await fn()
    } catch (error) {
      status(error.message, true)
    }
  }
}
async function refresh() {
  const own = await api(`/v1/sessions/${session()}/ownership`);
  $('ownership').textContent = own.state === 'human_owned' ? '人工服务中' : 'Agent 服务中';
  const messages = await api(`/v1/sessions/${session()}/human-messages`);
  $('human').replaceChildren();
  for (const message of messages.messages) {
    const p = document.createElement('p');
    p.textContent = message.content;
    $('human').append(p)
  }
  const drafts = await api(`/v1/sessions/${session()}/after-sale-intakes`);
  $('intakes').replaceChildren();
  for (const intake of drafts.intakes) {
    const card = document.createElement('p');
    card.textContent = `订单：${intake.fields.order_id || '待补充'}；原因：${intake.fields.reason || '待补充'}；${intake.status === 'withdrawn' ? '已撤回' : '已保存，执行仍需单独确认'}`;
    $('intakes').append(card);
  }
  const data = await api(`/v1/sessions/${session()}/actions`);
  $('actions').replaceChildren();
  for (const action of data.actions) {
    const card = document.createElement('div');
    card.className = 'card';
    const title = document.createElement('h3');
    title.textContent = ({
      create_after_sale_case: '创建售后工单',
      execute_after_sale_case: '执行售后处理',
      apply_refund: '申请退款',
      inventory_command: '库存操作'
    })[action.tool_name] || action.tool_name;
    const details = document.createElement('pre');
    details.textContent = Object.entries(action.arguments).filter(([key]) => !['user_confirmed', 'evidence_provided', 'contract_version'].includes(key)).map(([key, value]) => (({
      order_id: '订单号',
      reason: '售后原因',
      case_type: '售后类型',
      case_id: '工单号',
      evidence_ids: '证据编号',
      quantity: '数量',
      sku: '商品编号'
    })[key] || key) + '：' + (typeof value === 'object' ? JSON.stringify(value) : value)).join('\n');
    card.append(title, details);
    for (const [text, verb] of [
        ['确认并执行', 'confirm'],
        ['拒绝', 'reject']
      ]) {
      const button = document.createElement('button');
      button.textContent = text;
      button.onclick = handle(async () => {
        button.disabled = true;
        try {
          await api(`/v1/actions/${action.action_id}/${verb}`, 'POST', {
            session_id: $('session').value,
            ...(verb === 'confirm' ? {
              arguments_hash: action.arguments_hash
            } : {})
          });
          if (verb === 'confirm') {
            const result = await api(`/v1/actions/${action.action_id}/execute`, 'POST', {
              session_id: $('session').value
            });
            bubble(result.accepted ? '请求已受理，请查询最终状态。' : result.case ? '工单 ' + result.case.case_id + '：' + (labels[result.case.status] || result.case.status) : result.message || (result.success ? '操作已完成' : '操作未完成，请查看状态'));
            if (result.case) $('case').value = result.case.case_id
          }
          await refresh()
        } finally {
          button.disabled = false
        }
      });
      card.append(button)
    }
    $('actions').append(card)
  }
}
async function watch() {
  const watchedRun = runId;
  if (!watchedRun) return;
  const token = $('token').value;
  const sessionId = $('session').value;
  const slot = await client.scopeKey('run:' + sessionId, token);
  if (token !== $('token').value || sessionId !== $('session').value) return;
  const complete = await stream.watch(watchedRun, token, slot + ':cursor:' + watchedRun, event => {
    if (token !== $('token').value || sessionId !== $('session').value) return;
    $('events').textContent += event.event_type + '\n';
    status('执行进度：' + event.event_type);
  });
  if (!complete || slot !== await runSlot()) return;
  const result = await client.request(`/v1/runs/${watchedRun}`, 'GET', undefined, {}, token);
  if (slot !== await runSlot()) return;
  status('任务状态：' + (labels[result.status] || result.status) + (result.error_code ? '（' + result.error_code + '）' : ''));
  if (result.result && !displayedRuns.has(watchedRun)) {
    bubble(result.result.reply);
    displayedRuns.add(watchedRun);
  }
  await refresh();
}
$('connect').onclick = handle(async () => {
  sessionStorage.setItem('agentToken', $('token').value);
  sessionStorage.setItem('agentSession', $('session').value);
  stream.stop();
  runId = sessionStorage.getItem(await runSlot()) || '';
  await refresh();
  status('会话已连接')
});
$('send').onclick = handle(async () => {
  if ($('send').disabled) return;
  const text = $('message').value;
  const sessionId = $('session').value;
  const token = $('token').value;
  $('send').disabled = true;
  try {
    const slot = await client.scopeKey('run:' + sessionId, token);
    const result = await client.write('/v1/runs', {
      message: text,
      session_id: sessionId
    }, sessionId, token);
    sessionStorage.setItem(slot, result.run_id);
    if (slot !== await runSlot()) return;
    bubble('你：' + text);
    $('message').value = '';
    runId = result.run_id;
    await watch();
  } finally {
    $('send').disabled = false;
  }
});
$('cancel').onclick = handle(async () => {
  if (runId) {
    const result = await api(`/v1/runs/${runId}/cancel`, 'POST');
    status('取消状态：' + result.cancel_state)
  }
});
$('reconnect').onclick = handle(async () => {
  runId = sessionStorage.getItem(await runSlot()) || '';
  await refresh();
  await watch()
});
$('queryOrder').onclick = handle(async () => show(await api('/v1/orders/' + encodeURIComponent($('order').value)), 'orderResult'));
$('queryCase').onclick = handle(async () => show(await api('/v1/after-sales/' + encodeURIComponent($('case').value) + '/operation'), 'caseResult'));
$('search').onclick = handle(async () => {
  const result = await api('/v1/knowledge/search', 'POST', {
    query: $('query').value
  });
  $('citations').replaceChildren();
  const state = document.createElement('p');
  state.textContent = {
    answerable: '找到可引用证据',
    no_evidence: '没有适用证据',
    needs_clarification: '请补充地域、渠道或商品条件',
    policy_conflict: '政策存在冲突，请人工核对'
  } [result.outcome];
  $('citations').append(state);
  for (const hit of result.results) {
    const card = document.createElement('div');
    card.className = 'card';
    card.textContent = hit.doc + ' · ' + hit.section + '\n' + hit.text;
    const id = document.createElement('small');
    id.textContent = '版本 ' + hit.version_id + ' / 引用 ' + hit.chunk_id;
    const details = document.createElement('details'),
      summary = document.createElement('summary');
    summary.textContent = '查看引用定位与版本';
    details.append(summary, id);
    card.append(details);
    $('citations').append(card)
  }
});

for (const name of ['token', 'session']) $(name).addEventListener('input', () => {
  stream.stop();
  runId = '';
});
