import { requestJson } from "./api.js";
import { sendToAgent } from "./chat.js";

let adviceText;

export function initAdvice(el) {
  adviceText = el;
}

export async function loadAdvice() {
  const payload = await requestJson("/api/advice");
  adviceText.textContent = payload.advice;
}

export async function loadAdviceWithAI(tasks) {
  const taskList = tasks.map(t => `• ${t.title} (${t.priority || 'medium'}优先级${t.due_at ? ', 截止' + new Date(t.due_at).toLocaleString('zh-CN') : ''})`).join('\n');
  
  const message = `请分析我的当前任务状态，给我一个今天的工作建议。

我的任务列表：
${taskList || '暂无任务'}

请考虑：
1. 任务的优先级和截止时间
2. 任务的预估时长
3. 当前时间和精力状态
4. 任务的依赖关系

请给出 1-2 个具体的下一步行动建议，帮助我今天高效工作。`;

  await sendToAgent(message);
}

export async function loadReview() {
  const payload = await requestJson("/api/review");
  adviceText.textContent = payload.review;
}

export async function loadReviewWithAI(tasks) {
  const taskList = tasks.map(t => {
    const statusMap = { todo: '待办', doing: '进行中', done: '已完成', dropped: '已放弃' };
    return `• ${t.title} (${statusMap[t.status] || t.status}${t.due_at ? ', 截止' + new Date(t.due_at).toLocaleString('zh-CN') : ''})`;
  }).join('\n');
  
  const message = `请帮我复盘一下今天的工作状态。

任务列表：
${taskList || '暂无任务'}

请分析：
1. 已完成的任务和今天的成就
2. 进行中的任务
3. 过期/超期的任务及原因
4. 明天的工作重点
5. 改进建议

请给出一个简洁的复盘报告。`;

  await sendToAgent(message);
}
