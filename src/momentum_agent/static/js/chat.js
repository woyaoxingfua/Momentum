let chatLog, chatInput;
let onAfterChat = null;
let activeStream = null;


// 流式生成期间允许用户「停止」：中断 fetch，服务端会因客户端断开而结束这一轮
// （归档的 RunState/取消项里，客户端侧可先交付的正是这部分；工具副作用已执行的不会回滚，
//  与既有的 fail-closed 语义一致）。
function setCancelButtonVisible(visible) {
  if (typeof document === "undefined") return;
  const button = document.getElementById("chatCancelButton");
  if (button) button.hidden = !visible;
}

function beginStreaming() {
  const controller = new AbortController();
  activeStream = controller;
  setCancelButtonVisible(true);
  return controller;
}

function endStreaming() {
  activeStream = null;
  setCancelButtonVisible(false);
}

export function isChatStreaming() {
  return activeStream !== null;
}

export function cancelChat() {
  if (!activeStream) return false;
  activeStream.abort();
  return true;
}

export function initChat(log, input) {
  chatLog = log;
  chatInput = input;
  if (typeof document !== "undefined") {
    const cancelButton = document.getElementById("chatCancelButton");
    if (cancelButton) {
      cancelButton.addEventListener("click", () => { cancelChat(); });
      cancelButton.hidden = true;
    }
  }
}



// 回填持久化的对话历史：刷新页面后上下文仍然可见（历史由后端落库，重启也不丢）。
export async function loadChatHistory() {
  if (!chatLog) return [];
  // 当前会话已经有内容时不回填，避免与刚发出的消息重复。
  if (chatLog.querySelector(".message")) return [];
  // chat.js 走原生 fetch（不依赖 api.js），这里保持一致，并把 token 显式带上。
  const token = typeof localStorage !== "undefined" ? localStorage.getItem("momentum_token") : null;
  let payload;
  try {
    const response = await fetch("/api/chat/history", {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) return [];
    payload = await response.json();
  } catch {
    return [];
  }
  const turns = Array.isArray(payload?.turns) ? payload.turns : [];
  for (const turn of turns) {
    const role = turn?.role === "user" ? "user" : "assistant";
    const text = typeof turn?.content === "string" ? turn.content : "";
    if (text.trim()) addMessage(role, text);
  }
  return turns;
}

export function setAfterChat(fn) {
  onAfterChat = fn;
}

// 工具名称中文映射
const TOOL_LABELS = {
  create_task: "创建任务",
  create_plan: "规划任务",
  list_tasks: "列出任务",
  search_tasks: "搜索任务",
  get_task: "查询任务",
  get_overview: "获取总览",
  edit_task: "编辑任务",
  complete_task: "完成任务",
  start_task: "开始任务",
  drop_task: "放弃任务",
  postpone_task: "推迟任务",
  reopen_task: "恢复任务",
  create_subtask: "创建子任务",
  bulk_create_subtasks: "批量创建子任务",
  get_subtasks: "查询子任务",
  get_task_with_subtasks: "查询任务详情",
  add_task_dependency: "添加依赖",
  remove_task_dependency: "移除依赖",
  get_task_dependencies: "查询依赖",
  is_task_blocked: "检查阻塞",
  get_all_tags: "获取标签",
  get_tasks_by_tag: "按标签查询",
  add_tags_to_task: "添加标签",
  batch_complete_tasks: "批量完成",
  batch_start_tasks: "批量开始",
  save_note: "保存笔记",
  get_my_notes: "查看笔记",
  get_user_context: "获取上下文",
  get_daily_review: "每日回顾",
  get_system_status: "系统状态",
  generate_suggestion: "生成建议",
  get_daily_summary: "每日摘要",
  check_in: "签到",
  get_completion_stats: "完成统计",
  get_behavioral_profile: "行为画像",
  get_insights: "行为洞察",
  get_strategic_summary: "战略摘要",
  estimate_task_smart: "智能预估",
  get_next_best_task: "推荐任务",
  get_tasks_due_today: "今日任务",
  get_tasks_due_this_week: "本周任务",
  get_overdue_tasks: "逾期任务",
  get_doing_tasks: "进行中任务",
  get_current_weather: "查询天气",
  plan_outdoor_activity: "规划活动",
  set_user_location: "设置位置",
  get_user_location: "获取位置",
  get_location_info: "位置信息",
};

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const SAFE_MARKDOWN_SCHEMES = new Set(["http:", "https:", "mailto:", "tel:"]);

/**
 * 只允许 http/https/mailto/tel 以及站内相对地址，阻断 javascript:/data: 等可执行 scheme。
 * 空字符串表示不安全（调用方应退化为纯文本）。
 */
export function sanitizeMarkdownUrl(raw) {
  const value = String(raw ?? "").trim();
  if (!value) return "";
  // 浏览器解析 URL 时会忽略控制字符（\t \n \r 等），"jav\tascript:" 这类写法能绕过 scheme 检查
  if (/[\u0000-\u001F\u007F]/.test(value)) return "";
  const scheme = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(value);
  if (!scheme) return value; // 站内相对地址（/path、#hash、?query）
  return SAFE_MARKDOWN_SCHEMES.has(scheme[1].toLowerCase() + ":") ? value : "";
}
export function renderMarkdown(text) {
  if (!text) return "";

  // 保护未闭合的 markdown 标签 - 使用占位符
  // 这样可以先渲染 HTML，然后再恢复未闭合的标签
  const placeholders = [];
  let idx = 0;

  // 替换未闭合的 **...** (bold)
  let result = text.replace(/\*\*(.+?)(\*\*)?/g, (match, content, closing) => {
    if (closing) return match; // 已闭合，保留原样
    const ph = `\x00PH${idx++}\x00`;
    placeholders.push({ ph, html: `<strong>${escapeHtml(content)}</strong>` });
    return ph;
  });

  // 替换未闭合的 *...* (italic)
  result = result.replace(/\*(.+?)(\*)?/g, (match, content, closing) => {
    if (closing) return match; // 已闭合，保留原样
    const ph = `\x00PH${idx++}\x00`;
    placeholders.push({ ph, html: `<em>${escapeHtml(content)}</em>` });
    return ph;
  });

  // 替换未闭合的 `...` (inline code)
  result = result.replace(/`([^`]+)(`)?/g, (match, content, closing) => {
    if (closing) return match;
    const ph = `\x00PH${idx++}\x00`;
    placeholders.push({ ph, html: `<code>${escapeHtml(content)}</code>` });
    return ph;
  });

  // 转义 HTML
  result = escapeHtml(result);

  // 恢复占位符（未闭合的 markdown 标签）
  for (const { ph, html } of placeholders) {
    result = result.replace(ph, html);
  }

  // code blocks (完整格式)
  result = result.replace(/```(\w*)\n([\s\S]*?)```/g, "<pre><code>$2</code></pre>");

  // inline code
  result = result.replace(/`([^`]+)`/g, "<code>$1</code>");

  // headers
  result = result.replace(/^### (.+)$/gm, "<h4>$1</h4>");
  result = result.replace(/^## (.+)$/gm, "<h3>$1</h3>");
  result = result.replace(/^# (.+)$/gm, "<h2>$1</h2>");

  // unordered lists
  result = result.replace(/^[\s]*[-*+] (.+)$/gm, "<li>$1</li>");
  result = result.replace(/((?:<li>.*<\/li>\n?)+)/g, "<ul>$1</ul>");

  // ordered lists
  result = result.replace(/^[\s]*\d+\. (.+)$/gm, "<li>$1</li>");

  // links and images（先做 scheme 白名单，再输出）
  result = result.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, src) => {
    const safe = sanitizeMarkdownUrl(src);
    return safe ? `<img alt="${alt}" src="${safe}" />` : `<span>${alt}</span>`;
  });
  result = result.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, href) => {
    const safe = sanitizeMarkdownUrl(href);
    return safe ? `<a target="_blank" rel="noopener noreferrer" href="${safe}">${label}</a>` : `<span>${label}</span>`;
  });

  // blockquote
  result = result.replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>");

  // horizontal rule
  result = result.replace(/^---$/gm, "<hr>");

  // paragraphs: double newlines
  result = result.replace(/\n\n+/g, "</p><p>");
  result = "<p>" + result + "</p>";

  // clean up empty paragraphs and whitespace
  result = result.replace(/<p>\s*<\/p>/g, "");
  result = result.replace(/\n/g, "<br>");

  return result;
}

function addMessage(role, text) {
  const item = document.createElement("div");
  item.className = `message ${role}`;
  item.innerHTML = renderMarkdown(text);
  chatLog.appendChild(item);
  chatLog.scrollTop = chatLog.scrollHeight;
  return item;
}

// 创建工具指示器元素
function createToolIndicator(toolName) {
  const label = TOOL_LABELS[toolName] || toolName;
  const indicator = document.createElement("div");
  indicator.className = "tool-indicator";
  indicator.innerHTML = `<span class="tool-spinner"></span> ${label}…`;
  return indicator;
}

async function handleStreamResponse(response, agentItem) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let fullText = "";
  let toolIndicator = null;

  // 检查是否移动端
  const isMobile = window.getComputedStyle(document.querySelector(".coach")).display === "none";
  const mobileLog = document.getElementById("mobileChatLog");

  function syncMobile() {
    if (isMobile && mobileLog && chatLog) {
      mobileLog.innerHTML = chatLog.innerHTML;
      mobileLog.scrollTop = mobileLog.scrollHeight;
    }
  }

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        try {
          const event = JSON.parse(line.slice(6));

          if (event.type === "tool_start") {
            if (toolIndicator) toolIndicator.remove();
            toolIndicator = createToolIndicator(event.name);
            agentItem.appendChild(toolIndicator);
            chatLog.scrollTop = chatLog.scrollHeight;
            syncMobile();
          } else if (event.type === "tool_end") {
            if (toolIndicator) {
              toolIndicator.remove();
              toolIndicator = null;
            }
          } else if (event.type === "chunk") {
            fullText += event.text;
            if (toolIndicator) {
              toolIndicator.remove();
              toolIndicator = null;
            }
            agentItem.innerHTML = renderMarkdown(fullText || "…");
            chatLog.scrollTop = chatLog.scrollHeight;
            syncMobile();
          } else if (event.type === "error") {
            fullText = event.message || "出错了";
            agentItem.innerHTML = renderMarkdown(fullText);
            syncMobile();
          } else if (event.type === "done") {
            if (toolIndicator) {
              toolIndicator.remove();
              toolIndicator = null;
            }
            if (!fullText) {
              agentItem.innerHTML = renderMarkdown("…");
            }
            syncMobile();
          }
        } catch {
          // skip malformed lines
        }
      }
    }
  }
}

export async function sendChat(event) {
  event.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;
  chatInput.value = "";
  addMessage("user", message);

  const agentItem = addMessage("agent", "…");

  try {
    const token = localStorage.getItem("momentum_token");
    const headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const controller = beginStreaming();
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers,
      body: JSON.stringify({ message }),
      signal: controller.signal,
    });

    if (response.status === 401) {
      localStorage.removeItem("momentum_token");
      window.location.href = "/login.html";
      return;
    }

    if (!response.ok) {
      const err = await response.json();
      agentItem.textContent = err.error || "请求失败";
      return;
    }

    await handleStreamResponse(response, agentItem);
  } catch (err) {
    if (err && err.name === "AbortError") {
      const current = agentItem.textContent === "…" ? "" : agentItem.textContent;
      agentItem.textContent = `${current}（已停止生成）`;
    } else {
      agentItem.textContent = `连接失败：${err.message}`;
    }
  } finally {
    endStreaming();
  }

  if (onAfterChat) await onAfterChat();
}

export async function sendToAgent(message) {
  addMessage("user", message);

  const agentItem = addMessage("agent", "…");

  // 移动端：打开 Agent 面板
  const isMobile = window.getComputedStyle(document.querySelector(".coach")).display === "none";
  if (isMobile) {
    const mobileChatPanel = document.getElementById("mobileChatPanel");
    if (mobileChatPanel) {
      // 打开面板
      document.querySelectorAll(".mobile-panel").forEach(p => p.classList.remove("open"));
      document.querySelectorAll(".mobile-nav-item").forEach(b => b.classList.remove("active"));
      mobileChatPanel.classList.add("open");
      const agentNavBtn = document.querySelector('.mobile-nav-item[data-panel="chat"]');
      if (agentNavBtn) agentNavBtn.classList.add("active");
    }
  }

  try {
    const token = localStorage.getItem("momentum_token");
    const headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const controller = beginStreaming();
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers,
      body: JSON.stringify({ message }),
      signal: controller.signal,
    });

    if (response.status === 401) {
      localStorage.removeItem("momentum_token");
      window.location.href = "/login.html";
      return;
    }

    if (!response.ok) {
      const err = await response.json();
      agentItem.textContent = err.error || "请求失败";
      return;
    }

    await handleStreamResponse(response, agentItem);
  } catch (err) {
    if (err && err.name === "AbortError") {
      const current = agentItem.textContent === "…" ? "" : agentItem.textContent;
      agentItem.textContent = `${current}（已停止生成）`;
    } else {
      agentItem.textContent = `连接失败：${err.message}`;
    }
  } finally {
    endStreaming();
  }

  // 移动端：同步聊天记录
  if (isMobile) {
    const mobileLog = document.getElementById("mobileChatLog");
    if (mobileLog && chatLog) {
      mobileLog.innerHTML = chatLog.innerHTML;
      mobileLog.scrollTop = mobileLog.scrollHeight;
    }
  }

  if (onAfterChat) await onAfterChat();
}
