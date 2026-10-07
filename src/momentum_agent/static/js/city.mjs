function tokenHeaders(json = false) {
  const token = localStorage.getItem("momentum_token");
  return { ...(json ? { "Content-Type": "application/json" } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

async function apiJson(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { ...tokenHeaders(Boolean(options.body)), ...(options.headers || {}) } });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
}

function setStatus(element, message, isError = false) {
  if (!element) return;
  element.textContent = message;
  element.classList.toggle("error-text", isError);
}

export function bindCitySettings({ documentRef = document } = {}) {
  const controls = ["config", "mobileConfig"].map((prefix) => ({
    prefix,
    input: documentRef.getElementById(`${prefix}CityInput`),
    results: documentRef.getElementById(`${prefix}CityResults`),
    test: documentRef.getElementById(`${prefix}CityTest`),
    save: documentRef.getElementById(`${prefix}CitySave`),
    status: documentRef.getElementById(`${prefix}CityStatus`),
  }));
  const state = new Map(controls.map((control) => [control.prefix, { selected: null, timer: null, request: 0 }]));

  for (const control of controls) {
    if (!control.input) continue;
    const itemState = state.get(control.prefix);
    control.input.addEventListener("input", () => {
      itemState.selected = null;
      itemState.request += 1;
      clearTimeout(itemState.timer);
      const query = control.input.value.trim();
      if (query.length < 2) { control.results.replaceChildren(); return; }
      itemState.timer = setTimeout(async () => {
        const requestNo = ++itemState.request;
        try {
          const payload = await apiJson(`/api/cities/search?q=${encodeURIComponent(query)}`);
          if (requestNo !== itemState.request) return;
          control.results.replaceChildren();
          if (!payload.cities?.length) {
            const empty = documentRef.createElement("p");
            empty.className = "city-empty muted";
            empty.textContent = "没有找到匹配城市。";
            control.results.append(empty);
            return;
          }
          for (const city of payload.cities) {
            const option = documentRef.createElement("button");
            option.type = "button";
            option.className = "city-option";
            option.setAttribute("role", "option");
            option.textContent = [city.name, city.admin1, city.country].filter(Boolean).join(" · ");
            option.addEventListener("click", () => {
              itemState.selected = city;
              control.input.value = city.name;
              control.results.replaceChildren();
              setStatus(control.status, `已选择 ${option.textContent}；先测试天气，再保存为默认城市。`);
              for (const other of controls) {
                if (other.prefix !== control.prefix && other.input) other.input.value = city.name;
                const otherState = state.get(other.prefix);
                if (otherState) otherState.selected = city;
                if (other.results) other.results.replaceChildren();
              }
            });
            control.results.append(option);
          }
        } catch (error) {
          if (requestNo === itemState.request) setStatus(control.status, `城市搜索失败：${error.message}`, true);
        }
      }, 300);
    });

    control.test?.addEventListener("click", async () => {
      const city = itemState.selected;
      if (!city) { setStatus(control.status, "请先在搜索结果中选择城市。", true); return; }
      control.test.disabled = true;
      setStatus(control.status, "正在请求 Open-Meteo 实时天气…");
      try {
        const query = [city.name, city.country].filter(Boolean).join(", ");
        const weather = await apiJson(`/api/weather?city=${encodeURIComponent(query)}`);
        const country = weather.country ? ` · ${weather.country}` : "";
        setStatus(control.status, `天气可用：${weather.city}${country}，${weather.temperature}°C，${weather.condition_cn}（${weather.source}）。现在可以保存为默认城市。`);
      } catch (error) { setStatus(control.status, `天气测试失败：${error.message}`, true); }
      finally { control.test.disabled = false; }
    });

    control.save?.addEventListener("click", async () => {
      const city = itemState.selected;
      if (!city) { setStatus(control.status, "请先从搜索结果选择一个城市。", true); return; }
      control.save.disabled = true;
      try {
        await apiJson("/api/user/location", {
          method: "POST",
          body: JSON.stringify({ city: city.name, country: city.country || "", latitude: city.latitude, longitude: city.longitude }),
        });
        for (const item of controls) setStatus(item.status, `默认城市已保存到你的 Momentum 账户：${city.name}${city.country ? ` · ${city.country}` : ""}。`);
      } catch (error) { setStatus(control.status, `保存失败：${error.message}`, true); }
      finally { control.save.disabled = false; }
    });
  }

  return { controls, state };
}

export async function loadCityPreference(binding) {
  try {
    const payload = await apiJson("/api/user/location");
    for (const control of binding.controls) {
      if (control.input) control.input.value = payload.city || "";
      if (control.status && payload.city) setStatus(control.status, `账户默认城市：${payload.city}${payload.country ? ` · ${payload.country}` : ""}`);
    }
    return payload;
  } catch (error) {
    for (const control of binding.controls) setStatus(control.status, `无法读取默认城市：${error.message}`, true);
    return null;
  }
}
