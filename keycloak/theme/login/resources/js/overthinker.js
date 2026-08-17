document.addEventListener("DOMContentLoaded", () => {
  const title = document.getElementById("kc-page-title");
  const loginForm = document.getElementById("kc-form-login");
  if (title && loginForm) {
    const subtitle = document.createElement("p");
    subtitle.className = "overthinker-login-subtitle";
    subtitle.textContent = document.documentElement.lang.toLowerCase().startsWith("ko")
      ? "환영합니다! 계속하려면 로그인하세요."
      : "Welcome! Sign in to continue.";
    title.insertAdjacentElement("afterend", subtitle);
  }

  const wrapper = document.getElementById("kc-header-wrapper");
  if (!wrapper || wrapper.tagName === "A") return;

  const link = document.createElement("a");
  link.id = wrapper.id;
  link.className = wrapper.className;
  link.textContent = wrapper.textContent;
  link.href = "http://macbookpro:3000/";
  link.setAttribute("aria-label", "Overthinker 포털로 이동");
  wrapper.replaceWith(link);
});
