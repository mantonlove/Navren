import * as THREE from 'three';

const DURATION = 6.8;
const SHORE_LENGTH = 1.3;
const WALL_HEIGHT_SCALE = 30;
const NARROW_HALF_WIDTH = 1.6;
const WIDE_HALF_WIDTH = 5.0;
const ROUTE_START = 8;
const ROUTE_END = -42;
const clamp = (value) => THREE.MathUtils.clamp(value, 0, 1);
const smooth = (value) => {
  const t = clamp(value);
  return t * t * t * (t * (t * 6 - 15) + 10);
};
const segment = (seconds, from, to) => smooth((seconds - from) / (to - from));

/**
 * One uninterrupted welcome-to-login camera move. start() owns a monotonic
 * performance.now() clock; update(sceneSeconds) uses its argument only for
 * the small, continuing water movement after the 6.8 second transition.
 * boatScreen coordinates are CSS pixels relative to the canvas, and rotation
 * is in degrees. The caller retains its DOM button throughout the voyage.
 */
export function createWelcomeVoyage({ scene, camera, canvas, brand, water, resources = [] }) {
  const owned = [];
  const track = (resource) => { owned.push(resource); return resource; };
  const point = new THREE.Vector3();
  const projected = new THREE.Vector3();
  const quaternion = new THREE.Quaternion();
  const flatRotation = new THREE.Quaternion().setFromEuler(new THREE.Euler(-Math.PI / 2, 0, 0));
  const heading = new THREE.Quaternion();
  const verticalAxis = new THREE.Vector3(0, 1, 0);
  const buttonRotation = new THREE.Quaternion();
  const rollRotation = new THREE.Quaternion();
  const boatVertex = new THREE.Vector3();
  const worldButtonPoint = new THREE.Vector3();
  const labelPoint = new THREE.Vector3();
  const initialCamera = { position: new THREE.Vector3(), quaternion: new THREE.Quaternion(), filmOffset: 0 };
  const initialBrand = {
    position: brand.position.clone(), quaternion: brand.quaternion.clone(), scale: brand.scale.clone()
  };
  const initialBoat = new THREE.Vector3();
  const cameraTarget = new THREE.Vector3();
  const cameraPosition = new THREE.Vector3();
  const edgeLeft = new THREE.Vector3();
  const edgeRight = new THREE.Vector3();
  const contact = new THREE.Vector2();
  const topRay = new THREE.Raycaster();
  const topRayNdc = new THREE.Vector2(0, 1);
  const topWaterPoint = new THREE.Vector3();
  const waterPosition = water.mesh.geometry.getAttribute('position');
  const originalWaterPositions = new Float32Array(waterPosition.array);
  const expandedWaterPositions = new Float32Array(originalWaterPositions);
  const originalWaterSphere = water.mesh.geometry.boundingSphere?.clone();
  const extendWater = (value) => Math.abs(value) <= 40 ? value
    : Math.sign(value) * (40 + (Math.abs(value) - 40) * 6);
  for (let i = 0; i < expandedWaterPositions.length; i += 3) {
    expandedWaterPositions[i] = extendWater(originalWaterPositions[i]);
    expandedWaterPositions[i + 1] = extendWater(originalWaterPositions[i + 1]);
  }
  waterPosition.setUsage(THREE.DynamicDrawUsage);
  let lastWaterExpansion = 0;
  let started = false;
  let disposed = false;
  let startTime = 0;
  let startWidth = 1;
  let targetWidth = 154;
  let ratio = 44 / 154;
  let viewport = { width: 1, height: 1 };
  let completed = false;
  let settledAt = null;
  let reducedMotion = false;
  const frameQuery = new URLSearchParams(window.location.search).get('voyageFrame');
  const requestedFrame = frameQuery === null ? NaN : Number(frameQuery);
  // Local-only still-frame review does not change the normal 6.8s timeline.
  const reviewFrame = ['127.0.0.1', 'localhost', '[::1]'].includes(window.location.hostname)
    && Number.isFinite(requestedFrame) ? THREE.MathUtils.clamp(requestedFrame, 0, DURATION) : null;

  // A strong S is readable from the moving camera, while each bank keeps
  // the original logo stroke's width, bevels and outer silhouette. The two
  // existing meshes and their vertex correspondence never change.
  const logoMeshes = brand.children.filter((child) => child.isMesh);
  const islandFootprints = [];
  const openedFootprints = [];
  const islandTopLevels = [];
  const norm = Math.sqrt(1 + 0.65 * 0.65);
  const channelCenter = (z) => {
    if (z < ROUTE_END) {
      const d = ROUTE_END - z;
      return 0.603185789 * d * Math.exp(-d / 23);
    }
    if (z > ROUTE_START) {
      const d = z - ROUTE_START;
      return -0.603185789 * d * Math.exp(-d / 23);
    }
    return 4.8 * Math.sin((ROUTE_START - z) * Math.PI / 25);
  };
  const channelSlope = (z) => (channelCenter(z + 0.025) - channelCenter(z - 0.025)) / 0.05;
  function coastScaleForViewport() {
    return viewport.width < viewport.height ? 0.42 : 1;
  }
  function finalCameraConfiguration(portrait) {
    const coastScale = portrait ? 0.42 : 1;
    const follow = portrait ? 13 : 14.6;
    const ahead = portrait ? 4.6 : 5.5;
    const height = portrait ? 8.9 : 8.6;
    const pitch = THREE.MathUtils.degToRad(portrait ? 26 : 22);
    const x = channelCenter(ROUTE_END + follow) * coastScale;
    const z = ROUTE_END + follow;
    const targetX = THREE.MathUtils.lerp(channelCenter(ROUTE_END - ahead), channelCenter(ROUTE_END), 0.3) * coastScale;
    const targetZ = ROUTE_END - ahead;
    const horizontalDistance = Math.hypot(targetX - x, targetZ - z);
    return { x, y: height, z, pitch,
      yaw: Math.atan2(targetX - x, z - targetZ),
      targetX, targetY: height - Math.tan(pitch) * horizontalDistance, targetZ };
  }
  function normalAttribute(positions) {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.computeVertexNormals();
    const normal = geometry.getAttribute('normal').clone();
    const original = new Float32Array(normal.array);
    const coincident = new Map();
    for (let i = 0; i < normal.count; i += 1) {
      const offset = i * 3;
      const key = `${Math.round(positions[offset] * 1e5)},${Math.round(positions[offset + 1] * 1e5)},${Math.round(positions[offset + 2] * 1e5)}`;
      const bucket = coincident.get(key);
      if (bucket) bucket.push(i); else coincident.set(key, [i]);
    }
    for (const bucket of coincident.values()) {
      if (bucket.length < 2) continue;
      for (const index of bucket) {
        const offset = index * 3;
        const nx = original[offset], ny = original[offset + 1], nz = original[offset + 2];
        let x = 0, y = 0, z = 0;
        for (const neighbour of bucket) {
          const a = neighbour * 3;
          const dot = nx * original[a] + ny * original[a + 1] + nz * original[a + 2];
          if (dot < 0.65) continue;
          x += original[a]; y += original[a + 1]; z += original[a + 2];
        }
        const length = Math.hypot(x, y, z);
        if (length > 1e-7) normal.setXYZ(index, x / length, y / length, z / length);
      }
    }
    geometry.dispose();
    return normal;
  }
  const savedMorphs = logoMeshes.map((mesh, layer) => {
    const geometry = mesh.geometry;
    const source = geometry.getAttribute('position');
    const side = layer ? 1 : -1;
    const description = { x: [], z: [], shell: [], minimum: Infinity, maximum: -Infinity, side };
    const binCount = 512;
    for (let i = 0; i < source.count; i += 1) {
      const x = source.getX(i), y = source.getY(i), z = source.getZ(i);
      const u = (y - 0.65 * x - 3.25) / norm;
      const v = (x + 0.65 * (y - 3.25)) / norm;
      const cross = y - (layer ? 0.64 : 0.65) * x - (layer ? 1.90 : 4.60);
      const bend = 0.18 * Math.sin(x * 0.60) + 0.12 * Math.sin(y * 0.65)
        + (layer ? -0.17 : 0.19) * cross * cross;
      description.x[i] = -u * 6;
      description.z[i] = -v * 7.8 * SHORE_LENGTH;
      description.shell[i] = clamp((z - bend + 0.0621) / 0.1242);
      description.minimum = Math.min(description.minimum, description.z[i]);
      description.maximum = Math.max(description.maximum, description.z[i]);
    }
    const edge = new Float64Array(binCount).fill(side < 0 ? -Infinity : Infinity);
    const sectionStep = (description.maximum - description.minimum) / (binCount - 1);
    const include = (bin, x) => {
      if (bin < 0 || bin >= binCount) return;
      edge[bin] = side < 0 ? Math.max(edge[bin], x) : Math.min(edge[bin], x);
    };
    // Intersect actual source triangle edges with evenly spaced sections.
    // A snapped max(vertex) per bin creates a staircase in x, and stretches
    // neighbouring triangles into the conspicuous paper-like launch spikes.
    for (let triangle = 0; triangle < source.count; triangle += 3) {
      for (let e = 0; e < 3; e += 1) {
        const a = triangle + e, b = triangle + (e + 1) % 3;
        const za = description.z[a], zb = description.z[b];
        const xa = description.x[a], xb = description.x[b];
        if (Math.abs(zb - za) < 1e-8) {
          const bin = Math.round((za - description.minimum) / sectionStep);
          include(bin, xa); include(bin, xb);
          continue;
        }
        const from = Math.max(0, Math.ceil((Math.min(za, zb) - description.minimum) / sectionStep));
        const to = Math.min(binCount - 1, Math.floor((Math.max(za, zb) - description.minimum) / sectionStep));
        for (let bin = from; bin <= to; bin += 1) {
          const planeZ = description.minimum + bin * sectionStep;
          include(bin, THREE.MathUtils.lerp(xa, xb, (planeZ - za) / (zb - za)));
        }
      }
    }
    for (let i = 0; i < binCount; i += 1) {
      if (Number.isFinite(edge[i])) continue;
      let previous = i - 1, next = i + 1;
      while (previous >= 0 && !Number.isFinite(edge[previous])) previous -= 1;
      while (next < binCount && !Number.isFinite(edge[next])) next += 1;
      edge[i] = previous < 0 ? edge[next] : next >= binCount ? edge[previous]
        : THREE.MathUtils.lerp(edge[previous], edge[next], (i - previous) / (next - previous));
    }
    const filteredEdge = new Float64Array(binCount);
    const weights = [1, 4, 6, 4, 1];
    for (let i = 0; i < binCount; i += 1) {
      let x = 0;
      for (let tap = -2; tap <= 2; tap += 1) {
        x += edge[THREE.MathUtils.clamp(i + tap, 0, binCount - 1)] * weights[tap + 2];
      }
      filteredEdge[i] = x / 16;
    }
    const coastEdgeAt = (z) => {
      const position = THREE.MathUtils.clamp((z - description.minimum) / sectionStep, 0, binCount - 1);
      const index = Math.min(binCount - 2, Math.floor(position));
      return THREE.MathUtils.lerp(filteredEdge[index], filteredEdge[index + 1], position - index);
    };
    const extendShore = (z) => {
      // Both original strokes continue beyond the viewing frustum. Growth is
      // quadratic, so no visible kink appears at the end of the logo stroke.
      if (z < -36) {
        const d = -36 - z, span = -36 - description.minimum;
        return -36 - d - (214 - span) * (d / span) ** 2;
      }
      if (z > 30) {
        const d = z - 30, span = description.maximum - 30;
        return 30 + d + (130 - span) * (d / span) ** 2;
      }
      return z;
    };
    const island = new Float32Array(source.count * 3);
    const openIsland = new Float32Array(source.count * 3);
    let topLevel = -Infinity;
    for (let i = 0; i < source.count; i += 1) {
      const originalZ = description.z[i], shell = description.shell[i];
      const worldZ = extendShore(originalZ);
      const signedThickness = side * (description.x[i] - coastEdgeAt(originalZ));
      const strokeThickness = 0.43 * (signedThickness + Math.sqrt(signedThickness ** 2 + 0.0001));
      const topLean = shell ** 1.7 * (2.8 + 0.7 * Math.sin(worldZ * 0.14 + layer));
      const coastRelief = shell * (0.11 + 0.11 * Math.sin(worldZ * 0.47 + layer * 1.3));
      const height = -0.014 + shell * (0.86 + 0.07 * Math.sin(worldZ * 0.10 + layer)
        + 0.045 * Math.cos(worldZ * 0.25 + layer));
      topLevel = Math.max(topLevel, height);
      const bankX = channelCenter(worldZ) + side * (NARROW_HALF_WIDTH + strokeThickness + topLean + coastRelief);
      island[i * 3] = bankX;
      island[i * 3 + 1] = -worldZ;
      island[i * 3 + 2] = height;
      openIsland[i * 3] = bankX + side * (WIDE_HALF_WIDTH - NARROW_HALF_WIDTH);
      openIsland[i * 3 + 1] = -worldZ;
      openIsland[i * 3 + 2] = height;
    }
    islandFootprints[layer] = island;
    openedFootprints[layer] = openIsland;
    islandTopLevels[layer] = topLevel;
    const previous = {
      attributes: geometry.morphAttributes, relative: geometry.morphTargetsRelative,
      influences: mesh.morphTargetInfluences, dictionary: mesh.morphTargetDictionary,
      sphere: geometry.boundingSphere?.clone(), frustumCulled: mesh.frustumCulled
    };
    geometry.morphAttributes = {
      position: [new THREE.BufferAttribute(island, 3), new THREE.BufferAttribute(openIsland, 3),
        new THREE.BufferAttribute(new Float32Array(openIsland), 3)],
      normal: [normalAttribute(island), normalAttribute(openIsland), normalAttribute(openIsland)]
    };
    geometry.morphTargetsRelative = false;
    mesh.updateMorphTargets();
    mesh.frustumCulled = false;
    mesh.material.needsUpdate = true;
    return previous;
  });
  function rebuildLoginCoasts() {
    const portrait = viewport.width < viewport.height;
    const coastScale = coastScaleForViewport();
    const configuration = finalCameraConfiguration(portrait);
    const halfViewTan = Math.tan(THREE.MathUtils.degToRad(portrait ? 40 : 34) / 2) * camera.aspect;
    const gapFraction = portrait ? 0.65 : Math.max(0.46, 440 / viewport.width);
    const cosYaw = Math.cos(configuration.yaw), sinYaw = Math.sin(configuration.yaw);
    const cosPitch = Math.cos(configuration.pitch), sinPitch = Math.sin(configuration.pitch);
    logoMeshes.forEach((mesh, layer) => {
      const side = layer ? 1 : -1;
      const positions = new Float32Array(openedFootprints[layer]);
      for (let i = 0; i < positions.length; i += 3) {
        const worldZ = -positions[i + 1];
        const nearCompression = 3.0 * (1 - smooth((ROUTE_END + 6 - worldZ) / 6));
        positions[i] -= side * nearCompression;
        const worldX = positions[i] * coastScale;
        const worldY = positions[i + 2] * WALL_HEIGHT_SCALE;
        const dz = worldZ - configuration.z, dy = worldY - configuration.y;
        // This is a real, curved, outward leaning coast loft. The lower near
        // waterline is fixed; upper/far coasts open to provide a login vista.
        const upper = smooth((worldY + 0.08) / 0.23);
        const distant = smooth((-worldZ - 42) / 7);
        const inFront = smooth((configuration.z - worldZ - 3) / 5);
        const blend = (1 - (1 - upper) * (1 - distant)) * inFront;
        const denominator = cosYaw - side * gapFraction * halfViewTan * cosPitch * sinYaw;
        const wantedX = configuration.x + (side * gapFraction * halfViewTan
          * (-cosPitch * cosYaw * dz - sinPitch * dy) - sinYaw * dz) / denominator;
        const expansion = Math.max(0, side * (wantedX - worldX));
        positions[i] += side * expansion * blend / coastScale;
      }
      const positionAttribute = mesh.geometry.morphAttributes.position[2];
      positionAttribute.array.set(positions);
      positionAttribute.needsUpdate = true;
      mesh.geometry.morphAttributes.normal[2] = normalAttribute(positions);
      // r179 caches morph targets in a texture keyed by geometry and count.
      // Attribute.needsUpdate alone does not refresh that texture on resize.
      // Dispose only the GPU allocation; the same mesh/geometry/source arrays
      // remain alive and are uploaded again with the rebuilt perspective loft.
      mesh.geometry.dispose();
      mesh.material.needsUpdate = true;
    });
  }

  const boatGroup = new THREE.Group();
  boatGroup.name = 'Navren entrance button becoming a boat';
  boatGroup.visible = false;
  scene.add(boatGroup);
  const boatMaterial = track(new THREE.MeshPhysicalMaterial({
    color: 0x286997, metalness: 0.035, roughness: 0.29,
    clearcoat: 0.42, clearcoatRoughness: 0.18,
    envMapIntensity: 0.85, side: THREE.DoubleSide
  }));
  const boatGeometry = track(new THREE.BufferGeometry());
  const boat = new THREE.Mesh(boatGeometry, boatMaterial);
  boat.name = 'Continuous blue capsule to pointed hull';
  boat.castShadow = true;
  boat.frustumCulled = false;
  boatGroup.add(boat);
  const cabinMaterial = track(new THREE.MeshPhysicalMaterial({
    color: 0xc5e7ef, roughness: 0.38, metalness: 0.04,
    transparent: true, opacity: 0, depthWrite: false
  }));
  const cabin = new THREE.Mesh(track(new THREE.BoxGeometry(0.20, 0.072, 0.32)), cabinMaterial);
  cabin.position.set(0, 0.145, 0.065);
  cabin.name = 'Raised pale boat cabin';
  boatGroup.add(cabin);
  const roof = new THREE.Mesh(track(new THREE.BoxGeometry(0.224, 0.016, 0.25)), cabinMaterial);
  roof.position.set(0, 0.189, 0.075);
  roof.name = 'Fine raised cabin roof';
  boatGroup.add(roof);
  const glassMaterial = track(new THREE.MeshPhysicalMaterial({
    color: 0x165e80, metalness: 0.06, roughness: 0.12,
    clearcoat: 0.65, transparent: true, opacity: 0,
    depthWrite: false, side: THREE.DoubleSide, envMapIntensity: 1.1
  }));
  const windshield = new THREE.Mesh(track(new THREE.PlaneGeometry(0.172, 0.066)), glassMaterial);
  windshield.position.set(0, 0.165, -0.101);
  windshield.rotation.x = -0.32;
  windshield.name = 'Blue glass boat windshield';
  boatGroup.add(windshield);
  const railMaterial = track(new THREE.MeshPhysicalMaterial({
    color: 0xd9f0f5, roughness: 0.27, metalness: 0.05,
    transparent: true, opacity: 0, depthWrite: false
  }));
  const railPoints = [];
  for (let i = 0; i < 48; i += 1) {
    const angle = i / 48 * Math.PI * 2;
    railPoints.push(new THREE.Vector3(Math.sin(angle) * 0.229 * (1 + 0.25 * Math.cos(angle)),
      0.124, Math.cos(angle) * 0.63));
  }
  const rails = new THREE.Mesh(track(new THREE.TubeGeometry(
    new THREE.CatmullRomCurve3(railPoints, true), 64, 0.0085, 5, true
  )), railMaterial);
  rails.name = 'Slender pale boat gunwale';
  boatGroup.add(rails);

  function buildBoatGeometry() {
    const sides = 64;
    const initial = [], hull = [], indices = [];
    const ringScale = [0.93, 1, 1, 0.93];
    const ringDepth = [-0.018, -0.006, 0.006, 0.018];
    const hullScale = [0.17, 0.72, 1, 0.96];
    const hullHeight = [-0.09, -0.04, 0.055, 0.12];
    for (let ring = 0; ring < 4; ring += 1) {
      for (let i = 0; i < sides; i += 1) {
        const angle = i / sides * Math.PI * 2;
        const sine = Math.sin(angle), cosine = Math.cos(angle);
        const radius = ratio * 0.5;
        const x = Math.sign(sine) * (0.5 - radius) + sine * radius;
        initial.push(x * ringScale[ring], cosine * radius * ringScale[ring], ringDepth[ring]);
        // A tapered bow at local -z, with a gently rounded stern at +z.
        hull.push(sine * 0.235 * (1 + 0.25 * cosine) * hullScale[ring],
          hullHeight[ring], cosine * 0.65 * hullScale[ring]);
        if (ring < 3) {
          const a = ring * sides + i;
          const b = ring * sides + (i + 1) % sides;
          indices.push(a, b, b + sides, a, b + sides, a + sides);
        }
      }
    }
    const bottom = initial.length / 3;
    initial.push(0, 0, ringDepth[0]); hull.push(0, hullHeight[0], 0);
    const top = initial.length / 3;
    initial.push(0, 0, ringDepth[3]); hull.push(0, hullHeight[3], 0);
    for (let i = 0; i < sides; i += 1) {
      const next = (i + 1) % sides;
      indices.push(bottom, next, i, top, 3 * sides + i, 3 * sides + next);
    }
    boatGeometry.setAttribute('position', new THREE.Float32BufferAttribute(initial, 3));
    boatGeometry.setIndex(indices);
    boatGeometry.computeVertexNormals();
    const temporary = new THREE.BufferGeometry();
    temporary.setAttribute('position', new THREE.Float32BufferAttribute(hull, 3));
    temporary.setIndex(indices);
    temporary.computeVertexNormals();
    boatGeometry.morphAttributes = {
      position: [temporary.getAttribute('position').clone()],
      normal: [temporary.getAttribute('normal').clone()]
    };
    temporary.dispose();
    boatGeometry.computeBoundingSphere();
    boat.updateMorphTargets();
    boatMaterial.needsUpdate = true;
  }
  buildBoatGeometry();

  // The wake and spray are actual world-space geometry, so the same water
  // reflection camera also sees them. No full-screen effect or image is used.
  const foamMaterial = track(new THREE.MeshBasicMaterial({
    color: 0xedfcff, transparent: true, opacity: 0,
    depthWrite: false, side: THREE.DoubleSide
  }));
  const sprayGeometry = track(new THREE.SphereGeometry(1, 5, 3));
  const spray = new THREE.InstancedMesh(sprayGeometry, foamMaterial, 36);
  spray.name = 'Local water spray around the moving boat and login button';
  spray.frustumCulled = false;
  spray.visible = false;
  scene.add(spray);
  const transform = new THREE.Object3D();
  const wakeMaterial = track(new THREE.MeshBasicMaterial({
    color: 0xe7f8ff, transparent: true, opacity: 0,
    depthWrite: false, side: THREE.DoubleSide, vertexColors: true
  }));
  const wakeGeometry = track(new THREE.BufferGeometry());
  const wakePositions = [], wakeColors = [];
  function wakeVertex(side, t, edge) {
    const z = 1.18 + t * 4.82;
    const center = side * (0.42 + t * 0.88 + Math.sin(t * 9.2) * 0.023);
    const halfWidth = 0.021 + 0.009 * Math.sin(t * 7.3) ** 2;
    const alpha = 0.65 * (1 - t) ** 2 * (0.30 + 0.70 * Math.sin(t * 20.5 + side * 0.8) ** 2);
    wakePositions.push(center + edge * halfWidth, 0.018, z);
    wakeColors.push(1, 1, 1, alpha);
  }
  for (const side of [-1, 1]) {
    for (let strip = 0; strip < 24; strip += 1) {
      const a = strip / 24, b = (strip + 1) / 24;
      wakeVertex(side, a, -1); wakeVertex(side, a, 1); wakeVertex(side, b, 1);
      wakeVertex(side, a, -1); wakeVertex(side, b, 1); wakeVertex(side, b, -1);
    }
  }
  wakeGeometry.setAttribute('position', new THREE.Float32BufferAttribute(wakePositions, 3));
  wakeGeometry.setAttribute('color', new THREE.Float32BufferAttribute(wakeColors, 4));
  const wake = new THREE.Mesh(wakeGeometry, wakeMaterial);
  wake.name = 'Soft divided wake behind the boat';
  wake.visible = false;
  scene.add(wake);

  function resize() {
    const rect = canvas.getBoundingClientRect();
    viewport = { width: Math.max(1, rect.width), height: Math.max(1, rect.height) };
    const portrait = viewport.width < viewport.height;
    targetWidth = portrait ? 124 : 154;
    camera.aspect = viewport.width / viewport.height;
    camera.fov = portrait ? 40 : 34;
    camera.updateProjectionMatrix();
    rebuildLoginCoasts();
    if (started) update();
  }
  resize();

  function worldWidthAt(position, pixels) {
    point.copy(position).applyMatrix4(camera.matrixWorldInverse);
    const distance = Math.max(0.1, -point.z);
    return pixels / viewport.width * 2 * distance
      * Math.tan(THREE.MathUtils.degToRad(camera.fov) * 0.5) * camera.aspect;
  }

  function start(buttonRect, options = {}) {
    if (started || disposed || !buttonRect) return false;
    started = true;
    reducedMotion = !!options.reducedMotion;
    startTime = performance.now() - (reducedMotion ? DURATION * 1000 : 0);
    initialCamera.position.copy(camera.position);
    initialCamera.quaternion.copy(camera.quaternion);
    initialCamera.filmOffset = camera.filmOffset;
    camera.updateMatrixWorld(true);
    const canvasRect = canvas.getBoundingClientRect();
    const centerX = (buttonRect.left + buttonRect.width / 2 - canvasRect.left) / viewport.width;
    const centerY = (buttonRect.top + buttonRect.height / 2 - canvasRect.top) / viewport.height;
    const ray = new THREE.Raycaster();
    ray.setFromCamera(new THREE.Vector2(centerX * 2 - 1, 1 - centerY * 2), camera);
    // This face-camera plane exactly overlays the clicked DOM button, including
    // cases where it starts above the horizon and cannot hit the water plane.
    ray.ray.at(14, initialBoat);
    startWidth = worldWidthAt(initialBoat, buttonRect.width);
    ratio = buttonRect.height / buttonRect.width;
    buildBoatGeometry();
    boatGroup.position.copy(initialBoat);
    boatGroup.quaternion.copy(initialCamera.quaternion);
    boatGroup.scale.setScalar(startWidth);
    boatGroup.visible = true;
    spray.visible = true;
    wake.visible = true;
    canvas.dataset.voyageDuration = String(DURATION);
    update();
    return true;
  }

  function screenPoint(world, destination) {
    destination.copy(world).project(camera);
    destination.x = (destination.x * 0.5 + 0.5) * viewport.width;
    destination.y = (0.5 - destination.y * 0.5) * viewport.height;
    return destination;
  }

  function update(sceneSeconds = performance.now() / 1000) {
    if (!started || disposed) return { active: false, completed: false, seconds: 0, morph: 0, loginReady: false };
    const seconds = reviewFrame ?? Math.max(0, (performance.now() - startTime) / 1000);
    const t = Math.min(DURATION, seconds);
    completed = seconds >= DURATION;
    if (completed && settledAt === null) settledAt = Math.min(seconds, DURATION);
    const lift = segment(t, 0, 1.1);
    const sail = segment(t, 1.6, 5.4);
    // Quarter-turn the undistorted logo first. Strong S/longitudinal growth
    // begins only once the original thin strokes are lying on the water.
    const fall = segment(t, 0.04, 0.65);
    const boatFall = segment(t, 0.06, 1.05);
    const unfolding = segment(t, 0.65, 1.35);
    const wallRise = segment(t, 1.05, 1.55);
    const opening = segment(t, 0.85, 5.4);
    const cameraLower = segment(t, 1.6, 5.4);
    const form = segment(t, 5.4, 6.8);
    const hull = segment(t, 0.04, 0.78) * (1 - form);
    const waveTime = Number.isFinite(sceneSeconds) ? sceneSeconds : seconds;
    const portrait = viewport.width < viewport.height;
    const coastScale = coastScaleForViewport();
    const boatZ = THREE.MathUtils.lerp(ROUTE_START, ROUTE_END, sail);
    const routeX = channelCenter(boatZ) * coastScale;
    const yaw = Math.atan(channelSlope(boatZ) * coastScale);
    const finalCamera = finalCameraConfiguration(portrait);
    const cameraHeight = THREE.MathUtils.lerp(14, finalCamera.y, cameraLower);
    const followDistance = THREE.MathUtils.lerp(16, portrait ? 13 : 14.6, cameraLower);
    const aheadDistance = THREE.MathUtils.lerp(5.5, portrait ? 4.6 : 5.5, cameraLower);
    const viewAngle = THREE.MathUtils.lerp(35, portrait ? 26 : 22, cameraLower);
    const followingX = channelCenter(boatZ + followDistance) * coastScale;
    const lookingX = THREE.MathUtils.lerp(channelCenter(boatZ - aheadDistance), channelCenter(boatZ), 0.3) * coastScale;
    const horizontalDistance = Math.hypot(lookingX - followingX, followDistance + aheadDistance);
    cameraPosition.set(followingX, cameraHeight, boatZ + followDistance);
    cameraTarget.set(lookingX, cameraHeight - Math.tan(THREE.MathUtils.degToRad(viewAngle))
      * horizontalDistance, boatZ - aheadDistance);
    // Forward motion is observed through parallax and steering. Camera x is a
    // point behind the ship on the S, its target a separate point ahead: they
    // never share a straight locked x-axis or acquire a last-minute re-centre.
    camera.position.lerpVectors(initialCamera.position, cameraPosition, lift);
    camera.lookAt(cameraTarget);
    quaternion.copy(camera.quaternion);
    camera.quaternion.slerpQuaternions(initialCamera.quaternion, quaternion, lift);
    camera.fov = portrait ? 40 : 34;
    camera.filmOffset = THREE.MathUtils.lerp(initialCamera.filmOffset, 0, lift);
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld(true);

    brand.position.copy(initialBrand.position).multiplyScalar(1 - unfolding);
    brand.quaternion.slerpQuaternions(initialBrand.quaternion, flatRotation, fall);
    brand.scale.copy(initialBrand.scale);
    brand.scale.x *= THREE.MathUtils.lerp(1, coastScale, unfolding);
    brand.scale.z *= THREE.MathUtils.lerp(1, WALL_HEIGHT_SCALE, wallRise);
    logoMeshes.forEach((mesh) => {
      mesh.morphTargetInfluences[0] = unfolding * (1 - opening);
      mesh.morphTargetInfluences[1] = unfolding * opening * (1 - form);
      mesh.morphTargetInfluences[2] = unfolding * opening * form;
    });
    // Extend only the distant outer water grid. The dense near-field vertices,
    // analytic wave shader, material and reflection target keep their owners.
    if (unfolding !== lastWaterExpansion) {
      for (let i = 0; i < waterPosition.array.length; i += 3) {
        waterPosition.array[i] = THREE.MathUtils.lerp(originalWaterPositions[i], expandedWaterPositions[i], unfolding);
        waterPosition.array[i + 1] = THREE.MathUtils.lerp(originalWaterPositions[i + 1], expandedWaterPositions[i + 1], unfolding);
      }
      waterPosition.needsUpdate = true;
      water.mesh.geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 400);
      lastWaterExpansion = unfolding;
    }

    const bob = Math.sin(waveTime * 1.52 + boatZ * 0.19) * 0.027
      + Math.sin(waveTime * 2.24 - 0.3) * 0.009;
    point.set(routeX, THREE.MathUtils.lerp(0.24, 0.09, form) + bob, boatZ);
    boatGroup.position.lerpVectors(initialBoat, point, boatFall);
    heading.setFromAxisAngle(verticalAxis, yaw);
    boatGroup.quaternion.slerpQuaternions(initialCamera.quaternion, heading, boatFall);
    const forwardDirection = camera.getWorldDirection(point);
    const cameraYaw = Math.atan2(-forwardDirection.x, -forwardDirection.z);
    buttonRotation.setFromAxisAngle(verticalAxis, cameraYaw).multiply(flatRotation);
    // The closing capsule is horizontal, thin and physically on the sea. It
    // never turns upright to imitate a DOM button facing the camera.
    boatGroup.quaternion.slerp(buttonRotation, form);
    const roll = Math.sin(waveTime * 1.65) * 0.009 * boatFall;
    rollRotation.setFromAxisAngle(new THREE.Vector3(0, 0, 1), roll);
    boatGroup.quaternion.multiply(rollRotation);
    const vesselScale = portrait ? 0.85 : 1.7;
    const hullScale = THREE.MathUtils.lerp(startWidth, vesselScale, boatFall);
    const finalWidth = worldWidthAt(boatGroup.position, targetWidth);
    boatGroup.scale.setScalar(THREE.MathUtils.lerp(hullScale, finalWidth, form));
    // A little ground-plane depth keeps the thin physical capsule visible;
    // projection controls the actual hit layer, not an assumed 44px rectangle.
    boatGroup.scale.y *= THREE.MathUtils.lerp(1, 0.44 / ratio, form);
    boat.morphTargetInfluences[0] = hull;
    cabinMaterial.opacity = hull * 0.9;
    glassMaterial.opacity = hull * 0.80;
    railMaterial.opacity = hull * 0.68;
    for (const detail of [cabin, roof, windshield, rails]) detail.visible = hull > 0.001;
    boatGroup.updateMatrixWorld(true);

    wake.position.set(boatGroup.position.x, 0.015 + bob * 0.4, boatGroup.position.z);
    wake.rotation.y = yaw * (1 - form);
    wakeMaterial.opacity = boatFall * (1 - form) * 0.15;
    wake.scale.set(1, 1, 0.8 + Math.sin(waveTime * 0.9) * 0.08);
    foamMaterial.opacity = boatFall * (0.24 + Math.sin(waveTime * 1.1) * 0.06);
    for (let i = 0; i < 36; i += 1) {
      const cycle = ((waveTime * (0.43 + (i % 5) * 0.027) + i * 0.6180339) % 1 + 1) % 1;
      const angle = i * 2.399963;
      const radius = 0.58 + cycle * 0.55;
      const halfButton = finalWidth * 0.5;
      const x = THREE.MathUtils.lerp(Math.sin(angle) * radius,
        Math.sin(angle) * (halfButton + 0.13 + cycle * 0.22), form);
      const z = THREE.MathUtils.lerp(Math.cos(angle) * radius * 1.32,
        Math.cos(angle) * (0.23 + cycle * 0.34), form);
      const arc = Math.sin(cycle * Math.PI) * (0.11 + (i % 4) * 0.014);
      transform.position.set(boatGroup.position.x + x, 0.024 + arc,
        boatGroup.position.z + z);
      const size = (0.018 + (i % 3) * 0.006) * Math.sin(cycle * Math.PI);
      transform.scale.set(size, size * 1.6, size);
      transform.updateMatrix();
      spray.setMatrixAt(i, transform.matrix);
    }
    spray.instanceMatrix.needsUpdate = true;
    contact.set(boatGroup.position.x, boatGroup.position.z);
    if (water.setContact) water.setContact(contact, 1.25);
    else {
      water.mesh.material.uniforms.navrenContactCenter?.value.copy(contact);
      const strength = water.mesh.material.uniforms.navrenContactStrength;
      if (strength) strength.value = 1.25;
    }

    let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
    const vertexCount = boatGeometry.getAttribute('position').count;
    for (let index = 0; index < vertexCount; index += 1) {
      boat.getVertexPosition(index, boatVertex);
      boat.localToWorld(boatVertex);
      screenPoint(boatVertex, projected);
      left = Math.min(left, projected.x); right = Math.max(right, projected.x);
      top = Math.min(top, projected.y); bottom = Math.max(bottom, projected.y);
    }
    // Calibrate the physical closing plate to a CSS width using its real
    // perspective bounds. This also handles the user's native 1.38 aspect.
    if (form > 0) {
      const correction = THREE.MathUtils.lerp(1, targetWidth / Math.max(1, right - left), form);
      boatGroup.scale.multiplyScalar(correction);
      boatGroup.updateMatrixWorld(true);
      left = Infinity; top = Infinity; right = -Infinity; bottom = -Infinity;
      for (let index = 0; index < vertexCount; index += 1) {
        boat.getVertexPosition(index, boatVertex);
        boat.localToWorld(boatVertex);
        screenPoint(boatVertex, projected);
        left = Math.min(left, projected.x); right = Math.max(right, projected.x);
        top = Math.min(top, projected.y); bottom = Math.max(bottom, projected.y);
      }
    }
    screenPoint(boatGroup.localToWorld(edgeLeft.set(-0.5, 0, 0)), edgeLeft);
    screenPoint(boatGroup.localToWorld(edgeRight.set(0.5, 0, 0)), edgeRight);
    boat.getVertexPosition(vertexCount - 1, worldButtonPoint);
    boat.localToWorld(worldButtonPoint);
    screenPoint(worldButtonPoint, labelPoint);
    const boatScreen = {
      x: (left + right) / 2, y: (top + bottom) / 2,
      width: right - left, height: bottom - top,
      rotation: THREE.MathUtils.radToDeg(Math.atan2(edgeRight.y - edgeLeft.y, edgeRight.x - edgeLeft.x)),
      bounds: { left, top, right, bottom }
    };
    const labelAnchor = { x: labelPoint.x, y: labelPoint.y };
    const worldButtonAnchor = { x: worldButtonPoint.x, y: worldButtonPoint.y, z: worldButtonPoint.z };
    const phase = t < 1.1 ? 'lift-and-launch' : t < 1.6 ? 'channel-reveal'
      : t < 5.4 ? 'sailing' : completed ? 'login' : 'boat-to-login';
    canvas.dataset.voyagePhase = phase;
    canvas.dataset.voyageLogoFall = fall.toFixed(4);
    canvas.dataset.voyageIslandUnfolding = unfolding.toFixed(4);
    canvas.dataset.voyageWallRise = wallRise.toFixed(4);
    canvas.dataset.voyageProgress = clamp(seconds / DURATION).toFixed(4);
    canvas.dataset.voyageSeconds = t.toFixed(3);
    canvas.dataset.voyageBoatWorldX = boatGroup.position.x.toFixed(3);
    canvas.dataset.voyageBoatWorldY = boatGroup.position.y.toFixed(3);
    canvas.dataset.voyageBoatWorldZ = boatGroup.position.z.toFixed(3);
    canvas.dataset.voyageCameraWorldX = camera.position.x.toFixed(3);
    canvas.dataset.voyageCameraWorldY = camera.position.y.toFixed(3);
    canvas.dataset.voyageCameraWorldZ = camera.position.z.toFixed(3);
    canvas.dataset.voyageBoatScreenX = boatScreen.x.toFixed(3);
    canvas.dataset.voyageBoatScreenY = boatScreen.y.toFixed(3);
    canvas.dataset.voyageFarExpansion = form.toFixed(4);
    canvas.dataset.voyageUpperClearWidthTarget = portrait ? '0.65' : Math.max(0.46, 440 / viewport.width).toFixed(3);
    canvas.dataset.voyageChannelCenterX = routeX.toFixed(3);
    canvas.dataset.voyageChannelHalfWidth = ((NARROW_HALF_WIDTH + (WIDE_HALF_WIDTH - NARROW_HALF_WIDTH) * opening) * coastScale).toFixed(3);
    const halfVesselX = vesselScale * (0.245 * Math.abs(Math.cos(yaw)) + 0.65 * Math.abs(Math.sin(yaw)));
    const halfVesselZ = vesselScale * (0.65 * Math.abs(Math.cos(yaw)) + 0.245 * Math.abs(Math.sin(yaw)));
    let nearestLeft = -Infinity, nearestRight = Infinity;
    const channelWidth = (NARROW_HALF_WIDTH + (WIDE_HALF_WIDTH - NARROW_HALF_WIDTH) * opening) * coastScale;
    for (let section = -2; section <= 2; section += 1) {
      const centre = channelCenter(boatZ + halfVesselZ * section / 2) * coastScale;
      nearestLeft = Math.max(nearestLeft, centre - channelWidth);
      nearestRight = Math.min(nearestRight, centre + channelWidth);
    }
    const halfProjectedHull = THREE.MathUtils.lerp(halfVesselX, boatGroup.scale.x * 0.5, form);
    canvas.dataset.voyageChannelClearance = (Math.min(routeX - nearestLeft, nearestRight - routeX) - halfProjectedHull).toFixed(3);
    canvas.dataset.voyageBoatScreenWidth = boatScreen.width.toFixed(3);
    canvas.dataset.voyageBoatScreenHeight = boatScreen.height.toFixed(3);
    canvas.dataset.voyageLabelScreenX = labelAnchor.x.toFixed(3);
    canvas.dataset.voyageLabelScreenY = labelAnchor.y.toFixed(3);
    canvas.dataset.voyageBoatYaw = THREE.MathUtils.radToDeg(yaw).toFixed(3);
    canvas.dataset.voyageLeftIslandHeight = (islandTopLevels[0] * brand.scale.z).toFixed(3);
    canvas.dataset.voyageRightIslandHeight = (islandTopLevels[1] * brand.scale.z).toFixed(3);
    if (settledAt !== null) canvas.dataset.voyageSettledAt = settledAt.toFixed(3);
    const forward = camera.getWorldDirection(point);
    const actualViewAngle = THREE.MathUtils.radToDeg(Math.asin(-forward.y));
    canvas.dataset.voyageViewAngle = actualViewAngle.toFixed(2);
    canvas.dataset.voyageFinalTopRayDown = (actualViewAngle - camera.fov * 0.5).toFixed(2);
    topRay.setFromCamera(topRayNdc, camera);
    let topRayHitsWater = false;
    if (topRay.ray.direction.y < -1e-6) {
      topRay.ray.at(-camera.position.y / topRay.ray.direction.y, topWaterPoint);
      const extent = THREE.MathUtils.lerp(80, 280, unfolding);
      topRayHitsWater = Math.abs(topWaterPoint.x) <= extent && Math.abs(topWaterPoint.z) <= extent;
      canvas.dataset.voyageTopRayWaterX = topWaterPoint.x.toFixed(3);
      canvas.dataset.voyageTopRayWaterZ = topWaterPoint.z.toFixed(3);
    }
    canvas.dataset.voyageTopRayWithinWater = String(topRayHitsWater);
    const boatWorld = { x: boatGroup.position.x, y: boatGroup.position.y, z: boatGroup.position.z };
    return { active: !completed, completed, seconds, boatScreen, boatWorld, worldButtonAnchor, labelAnchor, morph: form, loginReady: completed };
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    scene.remove(boatGroup, spray, wake);
    waterPosition.array.set(originalWaterPositions);
    waterPosition.needsUpdate = true;
    water.mesh.geometry.boundingSphere = originalWaterSphere;
    brand.position.copy(initialBrand.position);
    brand.quaternion.copy(initialBrand.quaternion);
    brand.scale.copy(initialBrand.scale);
    logoMeshes.forEach((mesh, index) => {
      const saved = savedMorphs[index];
      mesh.geometry.morphAttributes = saved.attributes;
      mesh.geometry.morphTargetsRelative = saved.relative;
      mesh.morphTargetInfluences = saved.influences;
      mesh.morphTargetDictionary = saved.dictionary;
      mesh.geometry.boundingSphere = saved.sphere;
      mesh.frustumCulled = saved.frustumCulled;
    });
    owned.forEach((resource) => resource.dispose());
  }

  return { start, update, resize, dispose };
}
