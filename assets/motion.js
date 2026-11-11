/* III Awards Agency — scroll-linked motion (index.html, contact.html)
   - Scrub: progress follows scroll position; stop = stop, scroll up = reverse.
   - Only outer wrappers and plain text move; .svc-card / .resin-* / .form-card are never animated.
   - Initial hidden states are set by GSAP only, so content stays visible if JS or the CDN fails.
   - Desktop (≥769px): scrub. ≤768px: home gets a one-shot fade-up, contact stays static.
   - window.III_REDUCED_MOTION (assets/site.js): no scroll animation at all.
   Layout: shared anchor landing → home module → contact module. Each module skips itself
   when its elements are not on the page. */
(function () {
  var hasGsap = !!(window.gsap && window.ScrollTrigger);
  var animate = hasGsap && !window.III_REDUCED_MOTION;
  if (hasGsap) gsap.registerPlugin(ScrollTrigger);

  /* ---------- Shared: where the page starts ----------
     ScrollTrigger's refresh (it briefly scrolls to 0 to measure) and late font swaps can
     knock the browser off its own positioning, so this block puts the page where the visit
     should start, with smooth scrolling held off until layout settles:
     - Fresh visit with a hash (index.html#services, contact.html#evaluation): land on the target.
     - Back/forward or reload: return to the scroll position this history entry was left at.
       The position is kept in history.state because the browser's own restore can lose the
       race against ScrollTrigger's startup refresh.
     - Otherwise: nothing to do. */
  var html = document.documentElement;
  var STATE_KEY = 'iiiScrollY';
  var navEntry = window.performance && performance.getEntriesByType && performance.getEntriesByType('navigation')[0];
  var navType = navEntry ? navEntry.type : 'navigate';
  var arrive = null;

  if (navType === 'navigate') {
    var hashTarget = null;
    if (location.hash && location.hash.length > 1) {
      try { hashTarget = document.getElementById(decodeURIComponent(location.hash.slice(1))); } catch (e) {}
    }
    if (hashTarget) arrive = function () { hashTarget.scrollIntoView({ block: 'start' }); };
  } else if (navType === 'back_forward' || navType === 'reload') {
    var savedY = null;
    try { if (history.state && typeof history.state[STATE_KEY] === 'number') savedY = history.state[STATE_KEY]; } catch (e) {}
    if (savedY !== null) arrive = function () { window.scrollTo(0, savedY); };
  }

  var userMoved = false;
  function place() {
    if (userMoved) return;
    html.style.scrollBehavior = 'auto';
    arrive();
    if (hasGsap) ScrollTrigger.update();
  }
  if (arrive) {
    ['wheel', 'touchstart', 'keydown', 'mousedown'].forEach(function (ev) {
      window.addEventListener(ev, function () { userMoved = true; }, { once: true, passive: true });
    });
    html.style.scrollBehavior = 'auto';
    // Returning visitors go straight back, so the page does not sit at the top while it loads.
    if (navType !== 'navigate') arrive();
  }

  // Remember the scroll position on this history entry (used by the back/reload case above).
  var settled = false;
  var saveTimer;
  function remember() {
    if (!settled) return;
    try {
      var prev = history.state, next = {}, k;
      if (prev && typeof prev === 'object') for (k in prev) next[k] = prev[k];
      next[STATE_KEY] = Math.round(window.pageYOffset || 0);
      history.replaceState(next, '');
    } catch (e) {}
  }
  window.addEventListener('scroll', function () {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(remember, 200);
  }, { passive: true });
  window.addEventListener('pagehide', remember);

  function settle() {
    if (hasGsap) ScrollTrigger.refresh();
    if (arrive) {
      place();
      // Late layout shifts above the target (font swap, lazy images) would push it
      // off position: keep it pinned for a moment unless the visitor starts scrolling.
      var ro = window.ResizeObserver ? new ResizeObserver(place) : null;
      if (ro) ro.observe(document.body);
      setTimeout(function () { if (ro) ro.disconnect(); html.style.scrollBehavior = ''; }, 2000);
    }
    settled = true;
  }
  var loaded = new Promise(function (r) {
    if (document.readyState === 'complete') r(); else window.addEventListener('load', r);
  });
  var fontsReady = document.fonts && document.fonts.ready ? document.fonts.ready.catch(function () {}) : null;

  /* ---------- Home: 服務實績 (.stats-section) → 我們提供 (#services) ----------
     Values come from the approved sample: do not tune. */
  function home() {
    var stats = document.querySelector('.stats-section');
    var ambient = document.querySelector('.stats-ambient');
    var services = document.getElementById('services');
    if (!stats || !services) return;
    var head = services.querySelector('.section-head');
    var items = gsap.utils.toArray('.services-grid > li');
    if (!head || !items.length) return;

    var mm = gsap.matchMedia();

    // Desktop: scroll-linked
    mm.add('(min-width: 769px)', function () {
      // 1) Stats card recedes as it leaves: up, slightly smaller, dimmer → depth
      gsap.fromTo(stats,
        { yPercent: 0, scale: 1, opacity: 1 },
        {
          yPercent: -12, scale: 0.92, opacity: 0.45, ease: 'none',
          transformOrigin: '50% 100%',
          scrollTrigger: { trigger: stats, start: 'center 45%', end: 'bottom top', scrub: 0.6 }
        });

      // Ambient glow drifts slower than the card (parallax)
      if (ambient) {
        gsap.fromTo(ambient, { yPercent: 0 }, {
          yPercent: 25, ease: 'none',
          scrollTrigger: { trigger: stats, start: 'top bottom', end: 'bottom top', scrub: true }
        });
      }

      // 2) Services heading rises in while stats is still on screen (shared frame)
      gsap.fromTo(head,
        { y: 90, opacity: 0 },
        {
          y: 0, opacity: 1, ease: 'none',
          scrollTrigger: { trigger: services, start: 'top 100%', end: 'top 55%', scrub: 0.6 }
        });

      // 3) Cards 01–04: each <li> rises on its own scroll progress.
      //    Right-hand cards travel further → staggered, offset rows.
      items.forEach(function (li, i) {
        var extra = (i % 2) * 90;          // 02 / 04 lag behind 01 / 03
        var tl = gsap.timeline({
          scrollTrigger: { trigger: li, start: 'top 105%', end: 'top 62%', scrub: 0.7 }
        });
        tl.fromTo(li, { y: 140 + extra }, { y: 0, ease: 'none', duration: 1 }, 0)
          // opacity reaches 1 early, so the glow is complete for most of the entrance
          .fromTo(li, { opacity: 0 }, { opacity: 1, ease: 'none', duration: 0.35 }, 0);
      });
    });

    // Mobile: light one-shot reveal
    mm.add('(max-width: 768px)', function () {
      [head].concat(items).forEach(function (el) {
        gsap.from(el, {
          y: 32, opacity: 0, duration: 0.7, ease: 'power3.out',
          scrollTrigger: { trigger: el, start: 'top 88%', toggleActions: 'play none none none' }
        });
      });
    });

    // Keyboard focus on a card always shows it fully (no half-faded focus target)
    items.forEach(function (li) {
      li.addEventListener('focusin', function () {
        var r = li.getBoundingClientRect();
        if (r.top > window.innerHeight * 0.62) {
          window.scrollBy({ top: r.top - window.innerHeight * 0.45, behavior: 'auto' });
        }
      });
    });
  }

  /* ---------- Contact: .page-header → #evaluation ----------
     About half the home intensity. The header drifts up and dims as it leaves; the form
     title and intro line settle into place over the first half of that scroll.
     The form title is on screen when the page opens, so it is never faded and only starts
     12px low (a larger offset would crowd the first row of fields): translate only.
     #evaluation (.form-card) itself is never transformed, so the anchor target and the
     inputs stay put. ≤768px: static. */
  function contact() {
    var header = document.querySelector('.page-header');
    var card = document.getElementById('evaluation');
    if (!header || !card) return;
    var lead = [card.querySelector('#form-title'), card.querySelector('.form-intro')].filter(Boolean);

    gsap.matchMedia().add('(min-width: 769px)', function () {
      gsap.fromTo(header,
        { yPercent: 0, opacity: 1 },
        {
          yPercent: -6, opacity: 0.45, ease: 'none',
          scrollTrigger: { trigger: header, start: 'top top', end: 'bottom top', scrub: 0.6 }
        });

      if (lead.length) {
        gsap.fromTo(lead,
          { y: 12 },
          {
            y: 0, ease: 'none',
            scrollTrigger: {
              trigger: header, start: 'top top',
              end: function () { return '+=' + Math.round(header.offsetHeight / 2); },
              scrub: 0.6, invalidateOnRefresh: true
            }
          });
      }
    });
  }

  if (animate) { home(); contact(); }

  Promise.all([loaded, fontsReady]).then(settle);
})();
