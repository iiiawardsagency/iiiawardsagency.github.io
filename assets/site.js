// Shared: keyboard users and reduced-motion users get the system cursor
(function () {
  var root = document.documentElement;
  root.classList.add('js');   // CSS that depends on JS (sticky CTA) keys off this
  window.III_REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Tab' || e.key.indexOf('Arrow') === 0) root.classList.add('kbd');
  });
  document.addEventListener('mousemove', function () { root.classList.remove('kbd'); });

  // Marquees: page loops check this before moving
  window.III_marqueeStopped = function () {
    return window.III_REDUCED_MOTION || root.classList.contains('marquee-paused');
  };
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.marquee-toggle');
    if (!btn) return;
    var paused = root.classList.toggle('marquee-paused');
    document.querySelectorAll('.marquee-toggle').forEach(function (b) {
      b.setAttribute('aria-pressed', String(paused));
      b.setAttribute('aria-label', paused ? '播放輪播' : '暫停輪播');
    });
  });

  // Mobile menu
  document.addEventListener('DOMContentLoaded', function () {
    var toggle = document.getElementById('navToggle');
    var links = document.getElementById('navLinks');
    if (!toggle || !links) return;
    function setOpen(open) {
      toggle.classList.toggle('open', open);
      links.classList.toggle('open', open);
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? '關閉選單' : '開啟選單');
    }
    toggle.addEventListener('click', function () { setOpen(!links.classList.contains('open')); });
    links.addEventListener('click', function (e) {
      if (e.target.closest('a') || e.target === links) setOpen(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && links.classList.contains('open')) { setOpen(false); toggle.focus(); }
    });
  });

  // Sticky CTA (.sticky-cta): shown once the visitor is past the first half-screen (or at once
  // on a page too short to scroll), and lifted by however much of the footer is on screen.
  document.addEventListener('DOMContentLoaded', function () {
    var cta = document.querySelector('.sticky-cta');
    if (!cta) return;
    root.classList.add('has-sticky-cta');
    var footer = cta.closest('footer');
    var queued = false;
    function update() {
      queued = false;
      var vh = window.innerHeight;
      var max = Math.max(0, root.scrollHeight - vh);
      cta.classList.toggle('is-visible', (window.pageYOffset || 0) >= Math.min(vh, max) * 0.5);
      var lift = footer ? Math.max(0, vh - footer.getBoundingClientRect().top) : 0;
      cta.style.setProperty('--cta-lift', Math.round(lift) + 'px');
    }
    function queue() { if (!queued) { queued = true; requestAnimationFrame(update); } }
    window.addEventListener('scroll', queue, { passive: true });
    window.addEventListener('resize', queue);
    window.addEventListener('load', queue);
    if (window.ResizeObserver) new ResizeObserver(queue).observe(document.body);
    update();
  });
})();
