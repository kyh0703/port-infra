/* ──────────────────────────────────────────────────────────────
   스크롤하면 헤더 판이 사라지고 액션 묶음만 가운데로 옮겨간다.
   옆으로 미끄러뜨리지 않는다 — 시야 안에서 가로로 움직이는 요소는 글을 읽는
   동안 계속 시선을 끈다. 사라졌다가 새 위치에서 다시 나타나는 편이 조용하다.
   라이브러리는 쓰지 않는다: Headroom.js 류는 방향에 따른 숨김/보임만 하고
   위치 이동은 어차피 직접 짜야 한다.
   ────────────────────────────────────────────────────────────── */
(function () {
  var header = document.querySelector('.site-header');
  var dock = document.querySelector('.nav__actions');
  if (!header || !dock) return;

  var THRESHOLD = 64; // 히어로 아이브로우가 지나갈 즈음
  var FADE_MS = 170; // CSS .nav__actions transition 과 맞출 것
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var docked = window.scrollY > THRESHOLD;
  var ticking = false;
  var fadeTimer = null;

  // 중심 이동량을 잰다. 두 가지를 맞춰야 값이 맞는다.
  //  1) transform 이 걸린 상태로 재면 이미 옮겨진 위치가 기준이 돼 누적된다.
  //  2) 도킹되면 홈 버튼이 나타나 묶음이 넓어진다. 좁은 상태로 재면 그 폭의
  //     절반만큼 중심이 어긋난다. 그래서 도킹 레이아웃으로 두고 잰다.
  // 전환은 끄고 재며, 같은 태스크 안에서 되돌리므로 중간 상태는 그려지지 않는다.
  function measure() {
    var wasDocked = header.classList.contains('is-docked');
    var prevTransform = dock.style.transform;

    header.classList.add('no-anim');
    header.classList.add('is-docked');
    dock.style.transform = 'none';

    var rect = dock.getBoundingClientRect();
    var dx = window.innerWidth / 2 - (rect.left + rect.width / 2);
    dock.style.setProperty('--dock-dx', dx.toFixed(1) + 'px');

    dock.style.transform = prevTransform;
    header.classList.toggle('is-docked', wasDocked);
    void header.offsetWidth; // 되돌린 상태를 확정한 뒤 전환을 되살린다
    header.classList.remove('no-anim');
  }

  function commit() {
    header.classList.toggle('is-docked', docked);
    dock.classList.remove('is-fading');
  }

  function setDocked(next) {
    if (next === docked) return;
    docked = next;

    if (reduced) {
      commit();
      return;
    }

    // 사라진 뒤에 위치를 바꾸고 다시 나타난다. 이동 자체는 보이지 않는다.
    dock.classList.add('is-fading');
    clearTimeout(fadeTimer);
    fadeTimer = setTimeout(commit, FADE_MS);
  }

  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      setDocked(window.scrollY > THRESHOLD);
      ticking = false;
    });
  }

  measure();
  // 새로고침으로 이미 스크롤된 위치에서 시작할 수 있다. 첫 상태는 전환 없이 맞춘다.
  header.classList.toggle('is-docked', docked);

  window.addEventListener('scroll', onScroll, { passive: true });

  var resizeTimer;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(measure, 120);
  });
})();
