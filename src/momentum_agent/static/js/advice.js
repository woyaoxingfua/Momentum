import { requestJson } from "./api.js";

let adviceText;

export function initAdvice(el) {
  adviceText = el;
}

export async function loadAdvice() {
  const payload = await requestJson("/api/advice");
  adviceText.textContent = payload.advice;
}

export async function loadReview() {
  const payload = await requestJson("/api/review");
  adviceText.textContent = payload.review;
}
