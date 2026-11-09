import {
  RequestClient
} from './request_client.js';
const $ = id => document.getElementById(id);
const client = new RequestClient(() => $('token').value);
let flow = null,
  generation = 0,
  busy = false;
const labels = {
  collecting: '待补充资料',
  ready: '资料已保存',
  awaiting_confirmation: '等待创建确认',
  awaiting_execution_confirmation: '等待执行确认',
  withdrawn: '申请已撤回',
  pending: '待确认',
  confirmed: '已确认',
  completed: '操作已完成',
  processing: '业务处理中',
  unknown: '结果未知，等待对账',
  succeeded: '业务已成功',
  approved: '工单已审批',
  pending_review: '等待人工审批'
};
const caseTypes = {
  return_refund: '退货退款',
  exchange: '换货',
  quality_issue: '质量问题',
  missing_item: '缺少商品',
  logistics_damage: '物流破损',
  other: '其他'
};
const commands = {
  update: '保存资料',
  propose: '生成创建提案',
  propose_execution: '生成执行提案',
  withdraw: '撤回申请'
};
const status = (message, error = false) => {
  $('status').textContent = message;
  $('status').classList.toggle('error', error);
};
const path = () => '/v1/after-sale-intakes/' + flow.intake_id;

function controls() {
  for (const id of ['order', 'reason', 'caseType', 'evidenceIds']) $(id).disabled = !flow || busy;
  for (const id of ['save', 'propose', 'withdraw', 'executeProposal', 'refresh']) $(id).disabled = !flow || busy;
  for (const button of $('action').querySelectorAll('button')) button.disabled = busy;
  if (flow && !busy) {
    const started = ['completed', 'executing', 'unknown'].includes(flow.action?.status);
    const closed = flow.status === 'withdrawn';
    $('save').disabled = started || closed;
    $('withdraw').disabled = started || closed;
    $('propose').disabled = closed || Boolean(flow.action_id) || Boolean(flow.missing.length);
    $('executeProposal').disabled = flow.case?.case?.status !== 'approved' || ['pending', 'confirmed', 'executing', 'unknown', 'completed'].includes(flow.execution_action?.status);
  }
  $('create').disabled = busy;
  $('load').disabled = busy;
}

function handle(fn) {
  return async () => {
    if (busy) return;
    busy = true;
    controls();
    try {
      await fn();
    } catch (e) {
      status(e.message, true);
    } finally {
      busy = false;
      controls();
    }
  };
}

function render(data) {
  flow = data;
  $('intakeId').value = data.intake_id;
  $('order').value = data.fields.order_id;
  $('reason').value = data.fields.reason;
  $('caseType').value = data.fields.case_type;
  $('evidenceIds').value = data.fields.evidence_ids.join(',');
  $('progress').textContent = `${labels[data.status]||data.status} · 修订 ${data.revision} · ${data.missing.length?'还需补充：'+data.missing.map(k=>({order_id:'订单编号',reason:'售后原因'})[k]).join('、'):'资料完整'}`;
  $('action').replaceChildren();
  const action = data.execution_action || data.action;
  if (action) {
    const p = document.createElement('p');
    p.textContent = `${action.tool_name==='create_after_sale_case'?'创建售后工单':'执行售后工单'} · ${labels[action.status]||action.status}`;
    const detail = document.createElement('pre');
    detail.textContent = Object.entries(action.arguments).filter(([key]) => ["order_id", "case_id", "reason", "case_type"].includes(key)).map(([key, value]) => ({
      order_id: "订单",
      case_id: "工单",
      reason: "原因",
      case_type: "诉求"
    } [key]) + "：" + (key === "case_type" ? caseTypes[value] || value : value)).join("\n");
    $('action').append(p, detail);
    if (['pending', 'confirmed'].includes(action.status)) {
      const b = document.createElement('button');
      b.textContent = '确认并执行此操作';
      b.onclick = handle(async () => {
        const epoch = generation;
        await client.request(`/v1/actions/${action.action_id}/confirm`, 'POST', {
          session_id: flow.session_id,
          arguments_hash: action.arguments_hash
        });
        if (epoch !== generation) return;
        const result = await client.request(`/v1/actions/${action.action_id}/execute`, 'POST', {
          session_id: flow.session_id
        });
        if (epoch !== generation) return;
        if (result.success === false) throw new Error(result.error_code || result.error || '操作未成功，请刷新核对');
        await refresh();
        status('操作已提交，请以处理进度为准。');
      });
      $('action').append(b);
    }
  }
  $('business').textContent = data.business_state ? `当前业务状态：${labels[data.business_state]||data.business_state}` : '尚未创建业务工单';
  controls();
}
async function refresh() {
  const epoch = generation;
  const base = path();
  const data = await client.request(base);
  if (epoch !== generation) return;
  render(data);
  const history = await client.request(base + '/history');
  if (epoch !== generation) return;
  $('history').replaceChildren();
  for (const row of history.events) {
    const p = document.createElement('p');
    p.textContent = `修订 ${row.revision} · ${commands[row.command]||row.command}`;
    $('history').append(p);
  }
}
async function command(name, fields) {
  const epoch = generation;
  await client.write(path() + '/commands', {
    command: name,
    expected_revision: flow.revision,
    ...(fields === undefined ? {} : {
      fields
    })
  }, 'intake:' + flow.intake_id);
  if (epoch !== generation) return;
  await refresh();
  status('申请已更新。');
}
$('create').onclick = handle(async () => {
  const epoch = ++generation;
  const data = await client.write('/v1/after-sale-intakes', {
    session_id: $('session').value
  }, 'intake-create');
  if (epoch !== generation) return;
  render(data);
  await refresh();
  status('申请已创建，可以分次补充资料。');
});
$('load').onclick = handle(async () => {
  const epoch = ++generation;
  const data = await client.request('/v1/after-sale-intakes/' + encodeURIComponent($('intakeId').value));
  if (epoch !== generation) return;
  render(data);
  await refresh();
  status('已读取当前申请。');
});
$('save').onclick = handle(() => command('update', {
  order_id: $('order').value,
  reason: $('reason').value,
  case_type: $('caseType').value,
  evidence_ids: $('evidenceIds').value.split(',').map(x => x.trim()).filter(Boolean)
}));
$('propose').onclick = handle(() => command('propose'));
$('withdraw').onclick = handle(() => command('withdraw'));
$('executeProposal').onclick = handle(() => command('propose_execution'));
$('refresh').onclick = handle(refresh);
$('token').oninput = () => {
  generation++;
  flow = null;
  for (const id of ['action', 'history', 'business']) $(id).replaceChildren();
  for (const id of ['intakeId', 'order', 'reason', 'evidenceIds']) $(id).value = '';
  $('progress').textContent = '身份已切换，请重新查询申请。';
  controls();
};
