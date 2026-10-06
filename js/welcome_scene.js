import * as THREE from 'three';
import { Sky } from 'three/addons/objects/Sky.js';
import { createWelcomeWater } from './welcome_water.js';
import { createWelcomeLogin } from './welcome_login.js';

const canvas = document.getElementById('welcomeScene');
const fallback = document.getElementById('welcomeFallback');
const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
const state = { ready: false, frames: 0, renderer: 'static', error: null, disposed: false };
let renderer, scene, camera, water, environment, loginUI, raf = 0;
let lastFrame = 0, elapsed = 0, previousTime = 0;
let resizeObserver;
let renderSize = { width: 1, height: 1 };
const pointer = new THREE.Vector2();
const cameraOffset = new THREE.Vector2();
const cameraHome = new THREE.Vector3();
const lookAt = new THREE.Vector3();
const resources = [];
const frameTimes = [];
const frameIntervals = [];
let lastPresented = 0;
let gpuTimer, gpuQuery, gpuPending = false;

function setFailure(error) {
  state.error = error?.message || String(error);
  canvas.dataset.error = state.error;
  canvas.dataset.renderer = 'static';
  canvas.dataset.ready = 'false';
  canvas.style.opacity = '0';
  fallback.style.opacity = '1';
  cancelAnimationFrame(raf);
  console.error('Navren welcome scene:', error);
}

// Curves trace the two silhouettes of the actual 1280px brand icon.
function brandShapes() {
  const upper = new THREE.Shape();
  upper.moveTo(1138, 273);
  upper.bezierCurveTo(1143, 259, 1127, 258, 1100, 258);
  upper.bezierCurveTo(809, 242, 668, 344, 468, 510);
  upper.bezierCurveTo(367, 590, 296, 593, 238, 625);
  upper.bezierCurveTo(156, 672, 146, 790, 137, 943);
  upper.quadraticCurveTo(134, 958, 149, 953);
  upper.bezierCurveTo(445, 866, 548, 797, 685, 649);
  upper.bezierCurveTo(844, 476, 941, 377, 1117, 351);
  upper.quadraticCurveTo(1129, 350, 1131, 337);
  upper.lineTo(1138, 273);
  const lower = new THREE.Shape();
  lower.moveTo(130, 1066);
  lower.lineTo(126, 1092);
  lower.quadraticCurveTo(124, 1103, 142, 1107);
  lower.bezierCurveTo(442, 1167, 673, 1062, 850, 950);
  lower.bezierCurveTo(914, 911, 986, 903, 1017, 862);
  lower.bezierCurveTo(1062, 801, 1082, 674, 1104, 542);
  lower.quadraticCurveTo(1108, 519, 1088, 523);
  lower.bezierCurveTo(875, 546, 790, 646, 641, 804);
  lower.bezierCurveTo(479, 977, 336, 1018, 148, 1051);
  lower.quadraticCurveTo(132, 1054, 130, 1066);
  return [upper, lower];
}

function curvedThinGeometry(shape, layer) {
  const original = new THREE.ExtrudeGeometry(shape, {
    depth: 9, bevelEnabled: true, bevelThickness: 2.4, bevelSize: 2.4,
    bevelSegments: 5, steps: 1, curveSegments: 48
  });
  const sourcePositions = original.attributes.position;
  const sourceNormals = original.attributes.normal;
  const positions = [], normals = [], uvs = [];
  const scale = 0.009;
  const makePoint = (index) => ({
    p: new THREE.Vector3((sourcePositions.getX(index) - 640) * scale,
      (1120 - sourcePositions.getY(index)) * scale, (sourcePositions.getZ(index) - 4.5) * scale),
    n: new THREE.Vector3(sourceNormals.getX(index), -sourceNormals.getY(index), sourceNormals.getZ(index))
  });
  function middle(a, b) {
    return {p: a.p.clone().add(b.p).multiplyScalar(0.5), n: a.n.clone().add(b.n).normalize()};
  }
  function emit(v) {
    const {x, y, z} = v.p;
    const slope = layer ? 0.64 : 0.65;
    const cross = y - slope * x - (layer ? 1.90 : 4.60);
    const roll = layer ? -0.17 : 0.19;
    const bend = 0.18 * Math.sin(x * 0.60) + 0.12 * Math.sin(y * 0.65) + roll * cross * cross;
    const dx = 0.108 * Math.cos(x * 0.60) - 2 * roll * slope * cross;
    const dy = 0.078 * Math.cos(y * 0.65) + 2 * roll * cross;
    positions.push(x, y, z + bend);
    uvs.push((x + 5.5) / 11, (y + 0.5) / 8.5);
    const normal = new THREE.Vector3(v.n.x - dx * v.n.z, v.n.y - dy * v.n.z, v.n.z).normalize();
    normals.push(normal.x, normal.y, normal.z);
  }
  function subdivide(a, b, c, level) {
    const longest = Math.max(a.p.distanceToSquared(b.p), b.p.distanceToSquared(c.p), c.p.distanceToSquared(a.p));
    if (longest > 0.25 && level < 5) {
      const ab = middle(a, b), bc = middle(b, c), ca = middle(c, a);
      subdivide(a, ab, ca, level + 1); subdivide(ab, b, bc, level + 1);
      subdivide(ca, bc, c, level + 1); subdivide(ab, bc, ca, level + 1);
    } else { emit(a); emit(c); emit(b); }
  }
  // The pixel-to-world y flip reverses winding; a,c,b restores the front face.
  for (let i = 0; i < sourcePositions.count; i += 3) {
    subdivide(makePoint(i), makePoint(i + 1), makePoint(i + 2), 0);
  }
  original.dispose();
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute('normal', new THREE.Float32BufferAttribute(normals, 3));
  geometry.setAttribute('uv', new THREE.Float32BufferAttribute(uvs, 2));
  geometry.computeBoundingSphere();
  return geometry;
}

function createCoatingTexture() {
  const resolution = 512;
  const pixels = new Uint8Array(resolution * resolution * 4);
  let seed = 91381;
  for (let y = 0; y < resolution; y += 1) {
    for (let x = 0; x < resolution; x += 1) {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      const grain = seed / 4294967296;
      const broad = Math.sin(x * 0.021 + Math.sin(y * 0.017)) * Math.sin(y * 0.013 + 1.7);
      const value = Math.round(225 + broad * 12 + (grain - 0.5) * 10);
      const index = (y * resolution + x) * 4;
      pixels[index] = pixels[index + 1] = pixels[index + 2] = value;
      pixels[index + 3] = 255;
    }
  }
  const texture = new THREE.DataTexture(pixels, resolution, resolution, THREE.RGBAFormat);
  texture.name = 'Navren subtle satin-coating roughness';
  texture.magFilter = THREE.LinearFilter;
  texture.minFilter = THREE.LinearMipmapLinearFilter;
  texture.generateMipmaps = true;
  texture.needsUpdate = true;
  resources.push(texture);
  return texture;
}

function createBrand() {
  const group = new THREE.Group();
  group.name = 'Navren thin curved blue logo';
  const colors = [0x237eb8, 0x174f86];
  const coating = createCoatingTexture();
  brandShapes().forEach((shape, i) => {
    const geometry = curvedThinGeometry(shape, i);
    const material = new THREE.MeshPhysicalMaterial({
      color: colors[i], metalness: 0.035, roughness: i ? 0.34 : 0.28,
      roughnessMap: coating, clearcoat: 0.36, clearcoatRoughness: 0.22,
      envMapIntensity: 0.85,
      side: THREE.DoubleSide
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.castShadow = true;
    mesh.receiveShadow = false;
    mesh.name = i ? 'Navren lower curve' : 'Navren upper curve';
    group.add(mesh);
    resources.push(geometry, material);
  });
  group.position.set(3.6, -0.10, -4);
  group.rotation.y = 0.16;
  return group;
}

function createSky(sunDirection) {
  const sky = new Sky();
  sky.scale.setScalar(1800);
  const u = sky.material.uniforms;
  u.turbidity.value = 2.0;
  u.rayleigh.value = 1.7;
  u.mieCoefficient.value = 0.0025;
  u.mieDirectionalG.value = 0.78;
  u.sunPosition.value.copy(sunDirection).multiplyScalar(1800);
  const helpers = `
    float navrenHash(vec2 p) { return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
    float navrenNoise(vec2 p) {
      vec2 i=floor(p), f=fract(p); f=f*f*(3.0-2.0*f);
      return mix(mix(navrenHash(i),navrenHash(i+vec2(1,0)),f.x),
        mix(navrenHash(i+vec2(0,1)),navrenHash(i+vec2(1,1)),f.x),f.y);
    }
    float navrenCloud(vec2 p) {
      float n=0.0, a=0.5;
      const mat2 rotation=mat2(0.80,-0.60,0.60,0.80);
      for(int i=0;i<6;i++){ n+=a*navrenNoise(p); p=rotation*p*2.03+vec2(8.1,4.3); a*=0.5; }
      return n;
    }
  `;
  sky.material.fragmentShader = sky.material.fragmentShader.replace('void main() {', `${helpers}\nvoid main() {`)
    .replace('gl_FragColor = vec4( retColor, 1.0 );', `
      // A clear-day grade keeps atmospheric variation without white clipping.
      float skyHeight=smoothstep(0.0,0.42,max(direction.y,0.0));
      vec3 daylightChroma=mix(vec3(0.48,0.77,1.0),vec3(0.12,0.50,1.0),skyHeight);
      retColor *= mix(vec3(1.0),daylightChroma,0.67) * 0.68;
      vec2 cloudUv=direction.xz/(max(direction.y,0.015)+0.30)*2.25+vec2(3.7,1.1);
      vec2 warp=vec2(navrenNoise(cloudUv*0.8),navrenNoise(cloudUv*0.8+vec2(8.3,2.1)));
      cloudUv += (warp-0.5)*0.7;
      float density=navrenCloud(cloudUv);
      float cloud=smoothstep(0.48,0.69,density);
      cloud *= smoothstep(0.025,0.15,direction.y);
      float litDensity=navrenCloud(cloudUv+normalize(vSunDirection.xz)*0.22);
      float cloudLight=clamp(0.70+(density-litDensity)*2.2,0.34,1.0);
      vec3 cloudColor=mix(vec3(0.63,0.81,0.96),vec3(1.70,1.65,1.55),cloudLight);
      retColor=mix(retColor,cloudColor,cloud*0.72);
      gl_FragColor = vec4(retColor,1.0);
    `);
  resources.push(sky.geometry, sky.material);
  return sky;
}

function fitRenderResolution() {
  const nativeRatio = Math.min(window.devicePixelRatio || 1, 2);
  const pixelRatio = nativeRatio;
  if (Math.abs(renderer.getPixelRatio() - pixelRatio) > 0.001
      || canvas.width !== Math.floor(renderSize.width * pixelRatio)
      || canvas.height !== Math.floor(renderSize.height * pixelRatio)) {
    renderer.setPixelRatio(pixelRatio);
    renderer.setSize(renderSize.width, renderSize.height, false);
  }
  canvas.dataset.pixelRatio = pixelRatio.toFixed(3);
}

function resize() {
  if (!renderer || state.disposed) return;
  const rect = canvas.getBoundingClientRect();
  // The single-column account page hides the scene entirely.
  if (rect.width < 2 || rect.height < 2) return;
  const width = Math.max(1, rect.width), height = Math.max(1, rect.height);
  renderSize = { width, height };
  camera.aspect = width / height;
  const portrait = camera.aspect < 1;
  const auth = loginUI?.started;
  camera.fov = auth ? 34 : portrait ? 40 : 34;
  camera.filmOffset = auth || portrait ? 0 : -5.5;
  cameraHome.set(3.25, auth ? 3.7 : 2.9, auth ? 26 : portrait ? 35 : 22);
  lookAt.set(3.25, auth ? 3.5 : portrait ? 3.8 : 3.45, auth ? -4 : 0);
  camera.position.copy(cameraHome);
  camera.lookAt(lookAt);
  camera.updateProjectionMatrix();
  fitRenderResolution();
  renderer.shadowMap.needsUpdate = true;
  canvas.dataset.reflectionResolution = '1536';
  canvas.dataset.viewport = `${Math.round(width)}x${Math.round(height)}`;
  if (state.ready) renderFrame();
}

function renderFrame() {
  fitRenderResolution();
  water.update(elapsed);
  cameraOffset.lerp(pointer, motionPreference.matches || loginUI?.started ? 1 : 0.025);
  camera.position.copy(cameraHome);
  if (!loginUI?.started) {
    camera.position.x += cameraOffset.x * 0.32;
    camera.position.y += cameraOffset.y * 0.10;
  }
  camera.lookAt(lookAt);
  const gl = renderer.getContext();
  if (gpuPending && gl.getQueryParameter(gpuQuery, gl.QUERY_RESULT_AVAILABLE)) {
    if (!gl.getParameter(gpuTimer.GPU_DISJOINT_EXT)) {
      canvas.dataset.gpuMs = (gl.getQueryParameter(gpuQuery, gl.QUERY_RESULT) / 1e6).toFixed(1);
    }
    gpuPending = false;
  }
  const measureGpu = gpuTimer && !gpuPending && state.frames % 60 === 0;
  if (measureGpu) gl.beginQuery(gpuTimer.TIME_ELAPSED_EXT, gpuQuery);
  renderer.render(scene, camera);
  if (measureGpu) {
    gl.endQuery(gpuTimer.TIME_ELAPSED_EXT);
    gpuPending = true;
  }
  state.frames += 1;
  canvas.dataset.frames = String(state.frames);
  if (state.frames === 1 || state.frames % 60 === 0) {
    const quality = water.getQuality?.();
    if (quality) {
      canvas.dataset.waterVertices = String(quality.vertices);
      canvas.dataset.normalResolution = String(quality.normalResolution);
      canvas.dataset.reflectionTarget = quality.reflectionTargetCaptured ? 'captured' : 'pending';
      canvas.dataset.reflectionTargetWidth = String(quality.reflectionTargetWidth || 0);
      canvas.dataset.reflectionSamples = String(quality.reflectionTargetSamples || 0);
    }
  }
  if (state.frames % 30 === 0) {
    canvas.dataset.triangles = String(renderer.info.render.triangles);
    const mean = frameTimes.reduce((a, b) => a + b, 0) / Math.max(1, frameTimes.length);
    canvas.dataset.frameMs = mean.toFixed(1);
  }
}

function tick(now) {
  if (state.disposed || state.error) return;
  raf = requestAnimationFrame(tick);
  if (document.hidden || motionPreference.matches || (loginUI?.started && canvas.clientWidth < 2)) { previousTime = now; return; }
  const interval = 1000 / 60;
  if (now - lastFrame < interval - 0.5) return;
  if (previousTime) elapsed += Math.min((now - previousTime) / 1000, 0.1);
  previousTime = now;
  lastFrame = now - ((now - lastFrame) % interval);
  if (lastPresented) frameIntervals.push(now - lastPresented);
  lastPresented = now;
  if (frameIntervals.length > 120) frameIntervals.shift();
  const start = performance.now();
  renderFrame();
  frameTimes.push(performance.now() - start);
  if (frameTimes.length > 60) frameTimes.shift();
  if (state.frames % 60 === 0 && frameIntervals.length > 20) {
    const intervals = [...frameIntervals].sort((a, b) => a - b);
    const mean = frameIntervals.reduce((a,b) => a+b, 0) / frameIntervals.length;
    canvas.dataset.renderLoopFps = (1000 / mean).toFixed(1);
    canvas.dataset.intervalP95 = intervals[Math.floor(intervals.length * 0.95)].toFixed(1);
  }
}

function onPointer(event) {
  if (motionPreference.matches || canvas.clientWidth < canvas.clientHeight) return;
  const rect = canvas.getBoundingClientRect();
  pointer.set((event.clientX - rect.left) / rect.width - 0.5,
    0.5 - (event.clientY - rect.top) / rect.height);
}
function resetPointer() { pointer.set(0, 0); }
function visibilityChanged() { previousTime = 0; lastPresented = 0; frameIntervals.length = 0; }
function motionChanged() { visibilityChanged(); if (motionPreference.matches) { resetPointer(); renderFrame(); } }
function contextLost(event) { event.preventDefault(); setFailure(new Error('WebGL context lost')); }
function pageHidden(event) { if (!event.persisted) dispose(); }

function dispose() {
  if (state.disposed) return;
  state.disposed = true;
  cancelAnimationFrame(raf);
  resizeObserver?.disconnect();
  window.removeEventListener('resize', resize);
  window.removeEventListener('pointermove', onPointer);
  window.removeEventListener('pointerleave', resetPointer);
  window.removeEventListener('pagehide', pageHidden);
  document.removeEventListener('visibilitychange', visibilityChanged);
  motionPreference.removeEventListener('change', motionChanged);
  canvas.removeEventListener('webglcontextlost', contextLost);
  water?.dispose();
  loginUI?.dispose();
  resources.forEach(resource => resource.dispose());
  environment?.dispose();
  if (gpuQuery) renderer.getContext().deleteQuery(gpuQuery);
  renderer?.dispose();
}

window.NavrenWelcomeScene = Object.freeze({getState: () => ({...state, elapsed}), dispose});
try {
  renderer = new THREE.WebGLRenderer({canvas, antialias: true, alpha: false, powerPreference: 'high-performance'});
  gpuTimer = renderer.getContext().getExtension('EXT_disjoint_timer_query_webgl2');
  if (gpuTimer) gpuQuery = renderer.getContext().createQuery();
  canvas.dataset.gpuTimer = gpuTimer ? 'available' : 'unavailable';
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  // The logo and daylight are fixed; the water samples this same world-space
  // shadow as it moves, so the depth map only needs to be rendered once.
  renderer.shadowMap.autoUpdate = false;
  renderer.shadowMap.needsUpdate = true;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.80;
  renderer.debug.onShaderError = (gl, program, vertexShader, fragmentShader) => {
    console.error('Welcome shader diagnostics:', gl.getProgramInfoLog(program),
      gl.getShaderInfoLog(vertexShader), gl.getShaderInfoLog(fragmentShader));
    setFailure(new Error('Welcome scene shader compilation failed'));
  };
  scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0xb9d9eb, 0.0026);
  camera = new THREE.PerspectiveCamera(34, 1, 0.15, 4000);
  const sunDirection = new THREE.Vector3(-0.55, 0.65, 0.52).normalize();
  const sky = createSky(sunDirection);
  const skyScene = new THREE.Scene();
  skyScene.add(sky);
  const pmrem = new THREE.PMREMGenerator(renderer);
  environment = pmrem.fromScene(skyScene, 0);
  pmrem.dispose();
  scene.add(sky);
  scene.environment = environment.texture;
  scene.environmentIntensity = 0.72;
  const sun = new THREE.DirectionalLight(0xfff4e5, 3.0);
  const lightTarget = new THREE.Vector3(3.6, 3.5, -4);
  sun.position.copy(sunDirection).multiplyScalar(25).add(lightTarget);
  sun.target.position.copy(lightTarget);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, {left:-12,right:12,top:12,bottom:-12,near:0.5,far:65});
  sun.shadow.bias = -0.00012;
  sun.shadow.normalBias = 0.015;
  sun.shadow.camera.updateProjectionMatrix();
  scene.add(sun);
  scene.add(sun.target);
  scene.add(new THREE.HemisphereLight(0xcbdbe7, 0x70818a, 0.40));
  const softFill = new THREE.DirectionalLight(0xdce6ed, 0.65);
  softFill.position.set(-6, 9, 10);
  scene.add(softFill);
  const brand = createBrand();
  scene.add(brand);
  water = createWelcomeWater({sunDirection, sunColor: 0xfff6ec,
    skyColor: 0xb9d9eb, waterColor: 0x3c80ad, waveHeight: 0.11,
    waveSpeed: 0.42, detailStrength: 0.15, distortionScale: 1.6,
    reflectivity: 0.035, reflectionResolution: 1536,
    contactCenter: new THREE.Vector2(-0.1, -4)});
  scene.add(water.mesh);
  resize();
  renderFrame();
  if (!state.error) {
    state.ready = true;
    state.renderer = 'three-webgl';
    canvas.dataset.ready = 'true';
    canvas.dataset.renderer = state.renderer;
    canvas.dataset.scene = 'live-3d-logo-gerstner-water-planar-reflection';
    canvas.style.opacity = '1';
    fallback.style.opacity = '0';
    resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(canvas.parentElement);
    window.addEventListener('resize', resize);
    window.addEventListener('pointermove', onPointer, {passive: true});
    window.addEventListener('pointerleave', resetPointer);
    window.addEventListener('pagehide', pageHidden);
    document.addEventListener('visibilitychange', visibilityChanged);
    motionPreference.addEventListener('change', motionChanged);
    canvas.addEventListener('webglcontextlost', contextLost, false);
    raf = requestAnimationFrame(tick);
  }
} catch (error) { setFailure(error); }

loginUI = createWelcomeLogin({ canvas,
  onPhaseChange() {
    resetPointer();
    visibilityChanged();
    if (renderer && !state.error) resize();
  }
});
