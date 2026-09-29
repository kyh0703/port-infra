import {
  AmbientLight, BufferGeometry, DirectionalLight, Float32BufferAttribute,
  Group, LineBasicMaterial, LineLoop, Mesh, MeshStandardMaterial, PerspectiveCamera,
  Points, PointsMaterial, Scene, TorusKnotGeometry, Vector3, WebGLRenderer,
} from 'three';

const canvas = document.getElementById('thought-canvas');
if (canvas) createThoughtScene(canvas);

function createThoughtScene(canvas) {
  const host = canvas.parentElement;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  let renderer;
  try {
    renderer = new WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: 'low-power' });
  } catch (error) {
    // The static orbital outline remains visible on devices without WebGL.
    console.warn('overthinker: WebGL unavailable.', error);
    return;
  }

  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.5));
  const scene = new Scene();
  const camera = new PerspectiveCamera(36, 1, 0.1, 30);
  camera.position.set(0, 0, 7.8);
  const sculpture = new Group();
  sculpture.rotation.set(0.4, -0.35, -0.2);
  scene.add(sculpture);

  const knotMaterial = new MeshStandardMaterial({ color: 0xf0c6a0, metalness: 0.55, roughness: 0.32 });
  const knot = new Mesh(new TorusKnotGeometry(1.08, 0.055, 240, 10, 2, 3), knotMaterial);
  sculpture.add(knot);

  const orbitMaterial = new LineBasicMaterial({ color: 0x7a9aa5, transparent: true, opacity: 0.45 });
  for (let orbit = 0; orbit < 3; orbit += 1) {
    const positions = [];
    for (let step = 0; step < 160; step += 1) {
      const angle = step / 160 * Math.PI * 2;
      positions.push(new Vector3(Math.cos(angle) * 1.85, Math.sin(angle) * 1.85, 0));
    }
    const ring = new LineLoop(new BufferGeometry().setFromPoints(positions), orbitMaterial);
    ring.rotation.set(0.6 + orbit * 0.7, orbit * 0.8, orbit * 0.35);
    sculpture.add(ring);
  }

  const pointPositions = new Float32Array(180 * 3);
  for (let point = 0; point < 180; point += 1) {
    const angle = point * 2.399963;
    const height = 1 - (point / 179) * 2;
    const radius = Math.sqrt(1 - height * height);
    const distance = 2.1 + Math.sin(point * 12.7) * 0.25;
    pointPositions[point * 3] = Math.cos(angle) * radius * distance;
    pointPositions[point * 3 + 1] = height * distance;
    pointPositions[point * 3 + 2] = Math.sin(angle) * radius * distance;
  }
  const pointGeometry = new BufferGeometry();
  pointGeometry.setAttribute('position', new Float32BufferAttribute(pointPositions, 3));
  const pointMaterial = new PointsMaterial({ color: 0x9aafb3, size: 0.016, transparent: true, opacity: 0.65 });
  sculpture.add(new Points(pointGeometry, pointMaterial));

  scene.add(new AmbientLight(0xffffff, 2));
  const keyLight = new DirectionalLight(0xffdfbf, 4);
  keyLight.position.set(3, 4, 4);
  scene.add(keyLight);
  const rimLight = new DirectionalLight(0x8fcbe0, 3);
  rimLight.position.set(-3, -1, 2);
  scene.add(rimLight);

  let frameId = null;
  let lastFrame = 0;
  let visible = false;
  let pageActive = true;
  let contextLost = false;
  let disposed = false;
  const pointer = { x: 0, y: 0 };

  function render() {
    if (disposed || contextLost) return;
    renderer.render(scene, camera);
  }

  function resize() {
    const { width, height } = host.getBoundingClientRect();
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.position.z = camera.aspect < 1 ? 9 : 7.8;
    camera.updateProjectionMatrix();
    render();
  }

  function updatePalette() {
    const light = document.documentElement.dataset.theme === 'light';
    knotMaterial.color.set(light ? 0x9a5637 : 0xf0c6a0);
    orbitMaterial.color.set(light ? 0x416c78 : 0x7a9aa5);
    pointMaterial.color.set(light ? 0x476b76 : 0x9aafb3);
    render();
  }

  function frame(now) {
    frameId = requestAnimationFrame(frame);
    if (now - lastFrame < 1000 / 30) return;
    const delta = Math.min((now - lastFrame) / 1000, 0.05);
    lastFrame = now;
    sculpture.rotation.y += delta * 0.13;
    sculpture.rotation.x += (0.4 + pointer.y * 0.15 - sculpture.rotation.x) * 0.035;
    sculpture.rotation.z += (-0.2 + pointer.x * 0.12 - sculpture.rotation.z) * 0.035;
    render();
  }

  function syncAnimation() {
    if (frameId !== null) cancelAnimationFrame(frameId);
    frameId = null;
    if (disposed || contextLost || !visible || !pageActive || document.hidden) return;
    if (reducedMotion.matches) {
      render();
      return;
    }
    lastFrame = performance.now();
    frameId = requestAnimationFrame(frame);
  }

  function movePointer(event) {
    if (reducedMotion.matches || event.pointerType === 'touch') return;
    const bounds = canvas.getBoundingClientRect();
    pointer.x = Math.max(-1, Math.min(1, (event.clientX - bounds.left) / bounds.width * 2 - 1));
    pointer.y = Math.max(-1, Math.min(1, (event.clientY - bounds.top) / bounds.height * 2 - 1));
  }

  function loseContext(event) {
    event.preventDefault();
    contextLost = true;
    delete host.dataset.renderer;
    syncAnimation();
  }

  function restoreContext() {
    contextLost = false;
    resize();
    host.dataset.renderer = 'webgl';
    syncAnimation();
  }

  const resizeObserver = new ResizeObserver(resize);
  const paletteObserver = new MutationObserver(updatePalette);
  const intersectionObserver = new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    syncAnimation();
  });
  const section = host.closest('section');

  function hidePage(event) {
    pageActive = false;
    syncAnimation();
    if (event.persisted) return;
    disposed = true;
    resizeObserver.disconnect();
    paletteObserver.disconnect();
    intersectionObserver.disconnect();
    reducedMotion.removeEventListener('change', syncAnimation);
    document.removeEventListener('visibilitychange', syncAnimation);
    section.removeEventListener('pointermove', movePointer);
    canvas.removeEventListener('webglcontextlost', loseContext);
    canvas.removeEventListener('webglcontextrestored', restoreContext);
    window.removeEventListener('pagehide', hidePage);
    window.removeEventListener('pageshow', showPage);
    sculpture.traverse(object => object.geometry?.dispose());
    knotMaterial.dispose();
    orbitMaterial.dispose();
    pointMaterial.dispose();
    renderer.dispose();
  }

  function showPage() {
    pageActive = true;
    syncAnimation();
  }

  resizeObserver.observe(host);
  paletteObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  intersectionObserver.observe(host);
  section.addEventListener('pointermove', movePointer, { passive: true });
  reducedMotion.addEventListener('change', syncAnimation);
  document.addEventListener('visibilitychange', syncAnimation);
  canvas.addEventListener('webglcontextlost', loseContext);
  canvas.addEventListener('webglcontextrestored', restoreContext);
  window.addEventListener('pagehide', hidePage);
  window.addEventListener('pageshow', showPage);
  resize();
  updatePalette();
  host.dataset.renderer = 'webgl';
}
