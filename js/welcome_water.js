import * as THREE from 'three';
import { Water } from '/assets/vendor/welcome-three/examples/jsm/objects/Water.js';

// Water's planar camera supplies a real reflection of the logo and the sky.
// The surface below adds small geometric waves and matching world-space normals.
const WAVE_HEIGHT = 0.105;

function replaceOnce(source, needle, replacement, label) {
  if (source.indexOf(needle) < 0 || source.indexOf(needle) !== source.lastIndexOf(needle)) {
    throw new Error(`Navren water: the pinned Water r179.1 ${label} shader changed`);
  }
  return source.replace(needle, replacement);
}

function createNormalTexture(resolution = 512) {
  const pixels = new Uint8Array(resolution * resolution * 4);
  let seed = 0x6e617672;
  const random = () => {
    seed ^= seed << 13;
    seed ^= seed >>> 17;
    seed ^= seed << 5;
    return (seed >>> 0) / 4294967296;
  };
  // A broad random directional spectrum remains seamless while avoiding five
  // conspicuous repeating stripes. Three unrelated rotations are mixed below.
  const components = [];
  for (let i = 0; i < 48; i += 1) {
    const frequency = 2.5 * Math.pow(22, (i + random()) / 48);
    const angle = random() * Math.PI * 2;
    const frequencyX = Math.round(Math.cos(angle) * frequency);
    const frequencyZ = Math.round(Math.sin(angle) * frequency);
    const magnitude = Math.hypot(frequencyX, frequencyZ);
    const slope = 0.087 * Math.pow(magnitude / 5, -0.28);
    components.push([
      frequencyX, frequencyZ, slope / Math.max(magnitude, 1), random() * Math.PI * 2
    ]);
  }
  for (let y = 0; y < resolution; y += 1) {
    for (let x = 0; x < resolution; x += 1) {
      let slopeX = 0;
      let slopeZ = 0;
      for (const [frequencyX, frequencyZ, amplitude, phase] of components) {
        const theta = 2 * Math.PI * (frequencyX * x + frequencyZ * y) / resolution + phase;
        const derivative = amplitude * Math.cos(theta);
        slopeX += frequencyX * derivative;
        slopeZ += frequencyZ * derivative;
      }
      const inverseLength = 1 / Math.sqrt(slopeX * slopeX + slopeZ * slopeZ + 1);
      const index = (y * resolution + x) * 4;
      // Conventional tangent-space RGB: blue is the upward normal component.
      pixels[index] = Math.round((-slopeX * inverseLength * 0.5 + 0.5) * 255);
      pixels[index + 1] = Math.round((-slopeZ * inverseLength * 0.5 + 0.5) * 255);
      pixels[index + 2] = Math.round((inverseLength * 0.5 + 0.5) * 255);
      pixels[index + 3] = 255;
    }
  }
  const texture = new THREE.DataTexture(pixels, resolution, resolution, THREE.RGBAFormat);
  texture.name = 'Navren procedural water micro-normals';
  texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
  texture.magFilter = THREE.LinearFilter;
  texture.minFilter = THREE.LinearMipmapLinearFilter;
  texture.anisotropy = 4;
  texture.generateMipmaps = true;
  texture.needsUpdate = true;
  return texture;
}

const waveFunctions = /* glsl */ `
  uniform float navrenWaveHeight;
  uniform float navrenWaveSpeed;
  uniform vec2 navrenContactCenter;
  uniform float navrenContactStrength;

  void navrenWave(
    vec2 direction, float wavelength, float amplitude, float phase,
    vec2 waveXZ, vec2 waveDerivativeX, vec2 waveDerivativeZ,
    float envelope, vec2 envelopeDerivative,
    inout vec3 displaced, inout vec3 tangentX, inout vec3 tangentZ
  ) {
    float k = 6.28318530718 / wavelength;
    float a = amplitude * navrenWaveHeight;
    float theta = k * dot(direction, waveXZ)
      - sqrt(9.81 * k) * time * navrenWaveSpeed + phase;
    vec2 phaseDerivative = k * vec2(dot(direction, waveDerivativeX),
      dot(direction, waveDerivativeZ));
    float crest = sin(theta);
    float slope = cos(theta);
    float lateral = 0.38 * a;
    vec2 horizontalDerivative = lateral * (envelopeDerivative * slope
      - envelope * phaseDerivative * crest);
    vec2 verticalDerivative = a * (envelopeDerivative * crest
      + envelope * phaseDerivative * slope);
    displaced.xz += direction * lateral * envelope * slope;
    displaced.y += a * envelope * crest;
    tangentX += vec3(
      direction.x * horizontalDerivative.x,
      verticalDerivative.x,
      direction.y * horizontalDerivative.x
    );
    tangentZ += vec3(
      direction.x * horizontalDerivative.y,
      verticalDerivative.y,
      direction.y * horizontalDerivative.y
    );
  }

  void navrenContactRipple(
    vec2 baseXZ, inout vec3 displaced, inout vec3 tangentX, inout vec3 tangentZ
  ) {
    vec2 delta = baseXZ - navrenContactCenter;
    vec2 ellipse = vec2(1.0, 1.28);
    vec2 local = delta * ellipse;
    float radius = sqrt(dot(local, local) + 0.04);
    vec2 radiusDerivative = delta * ellipse * ellipse / radius;
    float envelope = exp(-0.50 * radius * radius);
    float irregularPhase = delta.x * 0.91 + delta.y * 0.53 + 0.4;
    float irregular = 0.86 + 0.14 * sin(irregularPhase);
    vec2 irregularDerivative = 0.14 * cos(irregularPhase) * vec2(0.91, 0.53);
    float pulse = 0.28 + 0.72 * pow(0.5 + 0.5 * sin(time * 0.67), 2.0);
    float amplitude = 0.009 * navrenContactStrength * pulse;
    float phase = radius * 3.4 - time * 1.12;
    float crest = sin(phase);
    float slope = cos(phase);
    displaced.y += amplitude * envelope * irregular * crest;
    vec2 derivative = amplitude * envelope * (
      irregularDerivative * crest
      + irregular * radiusDerivative * (3.4 * slope - radius * crest)
    );
    tangentX.y += derivative.x;
    tangentZ.y += derivative.y;
  }

  void navrenDisplace(vec3 base, out vec3 displaced, out vec3 surfaceNormal) {
    displaced = base;
    vec3 tangentX = vec3(1.0, 0.0, 0.0);
    vec3 tangentZ = vec3(0.0, 0.0, 1.0);
    float envelopePhase = dot(base.xz, vec2(0.045, 0.039));
    float envelope = 0.92 + 0.08 * sin(envelopePhase);
    vec2 envelopeDerivative = 0.08 * cos(envelopePhase) * vec2(0.045, 0.039);
    // A smooth two-dimensional phase field gently bends and breaks up crests.
    // Its Jacobian is used by both stages, so curved crests have true normals.
    float phaseA = dot(base.xz, vec2(0.24, 0.21)) + time * 0.018;
    float phaseB = dot(base.xz, vec2(-0.39, 0.51)) + 1.7 - time * 0.012;
    float sineA = sin(phaseA), sineB = sin(phaseB);
    float cosineA = cos(phaseA), cosineB = cos(phaseB);
    vec2 waveXZ = base.xz + vec2(1.15 * sineA + 0.45 * sineB,
      0.90 * sineB - 0.35 * sineA);
    vec2 waveDerivativeX = vec2(1.0 + 1.15 * 0.24 * cosineA - 0.45 * 0.39 * cosineB,
      -0.90 * 0.39 * cosineB - 0.35 * 0.24 * cosineA);
    vec2 waveDerivativeZ = vec2(1.15 * 0.21 * cosineA + 0.45 * 0.51 * cosineB,
      1.0 + 0.90 * 0.51 * cosineB - 0.35 * 0.21 * cosineA);
    navrenWave(vec2(0.93969262, 0.34202014), 8.8, 0.029, 0.40,
      waveXZ, waveDerivativeX, waveDerivativeZ, envelope, envelopeDerivative,
      displaced, tangentX, tangentZ);
    navrenWave(vec2(-0.46947156, 0.88294759), 6.2, 0.025, 2.10,
      waveXZ, waveDerivativeX, waveDerivativeZ, envelope, envelopeDerivative,
      displaced, tangentX, tangentZ);
    navrenWave(vec2(0.30901699, -0.95105652), 4.1, 0.023, 4.80,
      waveXZ, waveDerivativeX, waveDerivativeZ, envelope, envelopeDerivative,
      displaced, tangentX, tangentZ);
    navrenWave(vec2(-0.82903757, -0.55919290), 2.6, 0.018, 1.30,
      waveXZ, waveDerivativeX, waveDerivativeZ, envelope, envelopeDerivative,
      displaced, tangentX, tangentZ);
    navrenWave(vec2(0.70710678, 0.70710678), 1.45, 0.010, 3.70,
      waveXZ, waveDerivativeX, waveDerivativeZ, envelope, envelopeDerivative,
      displaced, tangentX, tangentZ);
    navrenContactRipple(base.xz, displaced, tangentX, tangentZ);
    surfaceNormal = normalize(cross(tangentZ, tangentX));
  }
`;

const noiseFunction = /* glsl */ `vec4 getNoise( vec2 uv ) {
    mat2 rotationA = mat2(0.8, 0.6, -0.6, 0.8);
    mat2 rotationB = mat2(-0.41614684, 0.90929743, -0.90929743, -0.41614684);
    vec2 uv0 = uv * 0.064 + vec2(time * 0.0045, time * 0.0061);
    vec2 uv1 = rotationA * uv * 0.110851 + vec2(-time * 0.0031, time * 0.0043);
    vec2 uv2 = rotationB * uv * 0.191827 + vec2(time * 0.0023, -time * 0.0027);
    vec3 normalA = texture2D(normalSampler, uv0).xyz * 2.0 - 1.0;
    vec3 normalB = texture2D(normalSampler, uv1).xyz * 2.0 - 1.0;
    vec3 normalC = texture2D(normalSampler, uv2).xyz * 2.0 - 1.0;
    // Rotate slopes back into the same world xz frame before blending.
    normalB.xy = normalB.xy * rotationA;
    normalC.xy = normalC.xy * rotationB;
    return vec4(normalA * 0.48 + normalB * 0.33 + normalC * 0.19, 1.0);
  }`;

function createWaterGeometry(size, segments) {
  const geometry = new THREE.PlaneGeometry(size, size, segments, segments);
  const position = geometry.attributes.position;
  const half = size / 2;
  for (let i = 0; i < position.count; i += 1) {
    const x = position.getX(i) / half;
    const y = position.getY(i) / half;
    // Keep the entire distant plane, but spend most vertices around the logo
    // and visible near water rather than the unresolvable far horizon.
    position.setXY(i, half * (0.28 * x + 0.72 * x * x * x),
      half * (0.20 * y + 0.80 * y * y * y));
  }
  position.needsUpdate = true;
  geometry.computeBoundingSphere();
  return geometry;
}

function boundedReflectionResolution(value) {
  return Math.round(THREE.MathUtils.clamp(Number(value) || 1536, 256, 2048));
}

/**
 * A horizontal reflective water surface with gentle, actual geometric waves.
 * Call update(timeInSeconds) immediately before rendering the main scene.
 */
export function createWelcomeWater({
  sunDirection = new THREE.Vector3(-0.45, 0.75, -0.48).normalize(),
  sunColor = 0xfff7ec,
  skyColor = 0xdae8ed,
  waterColor = 0x7697aa,
  size = 160,
  segments = 440,
  waveHeight = 0.104,
  waveSpeed = 0.43,
  detailStrength = 0.23,
  distortionScale = 2.6,
  reflectionResolution = 1536,
  reflectivity = 0.12,
  contactCenter = new THREE.Vector2(-0.1, 0),
  contactStrength = 1,
  fog = true
} = {}) {
  const geometry = createWaterGeometry(size, segments);
  const normals = createNormalTexture();
  const direction = sunDirection.isVector3
    ? sunDirection.clone().normalize()
    : new THREE.Vector3(...sunDirection).normalize();
  let desiredReflectionResolution = boundedReflectionResolution(reflectionResolution);
  const mesh = new Water(geometry, {
    textureWidth: desiredReflectionResolution,
    textureHeight: desiredReflectionResolution,
    waterNormals: normals,
    sunDirection: direction,
    sunColor,
    waterColor,
    distortionScale,
    alpha: 1,
    clipBias: 0.001,
    fog
  });
  mesh.name = 'Navren live reflective water';
  // Water's reflection code expects a local z normal; rotate the mesh, not geometry.
  mesh.rotation.x = -Math.PI / 2;
  mesh.frustumCulled = false;
  mesh.receiveShadow = true;
  const material = mesh.material;
  const mirrorTexture = material.uniforms.mirrorSampler.value;
  let reflectionTarget = null;
  material.name = 'Navren Gerstner water with planar scene reflection';
  material.uniforms.navrenWaveHeight = { value: Math.max(0, waveHeight) / WAVE_HEIGHT };
  material.uniforms.navrenWaveSpeed = { value: Math.max(0, waveSpeed) };
  material.uniforms.navrenDetailStrength = { value: Math.max(0, detailStrength) };
  material.uniforms.navrenReflectivity = { value: THREE.MathUtils.clamp(reflectivity, 0.02, 0.5) };
  material.uniforms.navrenSkyColor = { value: new THREE.Color(skyColor) };
  material.uniforms.navrenReflectionTexel = {
    value: new THREE.Vector2(1 / desiredReflectionResolution, 1 / desiredReflectionResolution)
  };
  material.uniforms.navrenContactCenter = {
    value: contactCenter.isVector2 ? contactCenter.clone() : new THREE.Vector2(...contactCenter)
  };
  material.uniforms.navrenContactStrength = { value: THREE.MathUtils.clamp(contactStrength, 0, 1.5) };

  try {
    material.vertexShader = replaceOnce(
      material.vertexShader,
      'void main() {',
      `varying vec2 navrenBaseXZ;\n${waveFunctions}\nvoid main() {`,
      'vertex entry'
    );
    const projection = [
      '\t\t\t\t\tmirrorCoord = modelMatrix * vec4( position, 1.0 );',
      '\t\t\t\t\tworldPosition = mirrorCoord.xyzw;',
      '\t\t\t\t\tmirrorCoord = textureMatrix * mirrorCoord;',
      '\t\t\t\t\tvec4 mvPosition =  modelViewMatrix * vec4( position, 1.0 );',
      '\t\t\t\t\tgl_Position = projectionMatrix * mvPosition;'
    ].join('\n');
    material.vertexShader = replaceOnce(material.vertexShader, projection, /* glsl */ `
      vec3 baseWorld = (modelMatrix * vec4(position, 1.0)).xyz;
      vec3 displacedWorld;
      vec3 vertexSurfaceNormal;
      navrenBaseXZ = baseWorld.xz;
      navrenDisplace(baseWorld, displacedWorld, vertexSurfaceNormal);
      worldPosition = vec4(displacedWorld, 1.0);
      mirrorCoord = textureMatrix * worldPosition;
      vec4 mvPosition = viewMatrix * worldPosition;
      gl_Position = projectionMatrix * mvPosition;`, 'vertex projection');
    material.vertexShader = replaceOnce(
      material.vertexShader,
      '#include <beginnormal_vertex>\n\t\t\t\t#include <defaultnormal_vertex>',
      'vec3 transformedNormal = (viewMatrix * vec4(vertexSurfaceNormal, 0.0)).xyz;',
      'vertex normals'
    );
    const noiseStart = material.fragmentShader.indexOf('vec4 getNoise( vec2 uv ) {');
    const noiseEnd = material.fragmentShader.indexOf('\n\t\t\t\tvoid sunLight', noiseStart);
    if (noiseStart < 0 || noiseEnd < 0) {
      throw new Error('Navren water: the pinned Water r179.1 noise shader changed');
    }
    material.fragmentShader = material.fragmentShader.slice(0, noiseStart)
      + noiseFunction + '\n' + material.fragmentShader.slice(noiseEnd);
    material.fragmentShader = replaceOnce(
      material.fragmentShader,
      'varying vec4 worldPosition;',
      /* glsl */ `varying vec4 worldPosition;
      varying vec2 navrenBaseXZ;
      uniform float navrenDetailStrength;
      uniform float navrenReflectivity;
      uniform vec3 navrenSkyColor;
      uniform vec2 navrenReflectionTexel;`,
      'fragment varyings'
    );
    material.fragmentShader = replaceOnce(
      material.fragmentShader,
      'vec3 surfaceNormal = normalize( noise.xzy * vec3( 1.5, 1.0, 1.5 ) );',
      /* glsl */ `vec3 detailNormal = normalize(noise.xzy);
      vec3 unusedDisplaced;
      vec3 analyticSurfaceNormal;
      navrenDisplace(vec3(navrenBaseXZ.x, 0.0, navrenBaseXZ.y),
        unusedDisplaced, analyticSurfaceNormal);
      vec3 surfaceNormal = normalize(analyticSurfaceNormal
        + vec3(detailNormal.x, 0.0, detailNormal.z) * navrenDetailStrength);`,
      'fragment normals'
    );
    material.fragmentShader = replaceOnce(
      material.fragmentShader,
      'void main() {',
      `${waveFunctions}\nvoid main() {`,
      'fragment wave functions'
    );
    material.fragmentShader = replaceOnce(
      material.fragmentShader,
      'vec4 noise = getNoise( worldPosition.xz * size );',
      'vec4 noise = getNoise(navrenBaseXZ * size);',
      'world micro-normal coordinates'
    );
    material.fragmentShader = replaceOnce(
      material.fragmentShader,
      'sunLight( surfaceNormal, eyeDirection, 100.0, 2.0, 0.5, diffuseLight, specularLight );',
      'sunLight( surfaceNormal, eyeDirection, 220.0, 0.42, 0.22, diffuseLight, specularLight );',
      'sun highlights'
    );
    material.fragmentShader = replaceOnce(
      material.fragmentShader,
      'vec2 distortion = surfaceNormal.xz * ( 0.001 + 1.0 / distance ) * distortionScale;',
      'vec2 distortion = surfaceNormal.xz * distortionScale * (0.00045 + 0.55 / (distance + 6.0));',
      'bounded reflection distortion'
    );
    material.fragmentShader = replaceOnce(
      material.fragmentShader,
      'vec3 reflectionSample = vec3( texture2D( mirrorSampler, mirrorCoord.xy / mirrorCoord.w + distortion ) );',
      /* glsl */ `vec2 reflectionUv = mirrorCoord.xy / mirrorCoord.w + distortion;
      vec2 softOffset = navrenReflectionTexel * vec2(0.81, 0.37)
        * (0.20 + smoothstep(18.0, 70.0, distance) * 0.45);
      vec3 reflectionSample = texture2D(mirrorSampler, reflectionUv).rgb * 0.72
        + texture2D(mirrorSampler, reflectionUv + softOffset).rgb * 0.14
        + texture2D(mirrorSampler, reflectionUv - softOffset).rgb * 0.14;`,
      'reflection sampling'
    );
    material.fragmentShader = replaceOnce(
      material.fragmentShader,
      'float rf0 = 0.3;',
      'float rf0 = navrenReflectivity;',
      'Fresnel'
    );
    material.fragmentShader = replaceOnce(
      material.fragmentShader,
      'vec3 albedo = mix( ( sunColor * diffuseLight * 0.3 + scatter ) * getShadowMask(), ( vec3( 0.1 ) + reflectionSample * 0.9 + reflectionSample * specularLight ), reflectance);',
      'vec3 albedo = mix((sunColor * diffuseLight * 0.18 + scatter) * getShadowMask(), (vec3(0.008) + reflectionSample * 0.985 + reflectionSample * specularLight), reflectance);',
      'water optical balance'
    );
    material.fragmentShader = replaceOnce(
      material.fragmentShader,
      'vec3 outgoingLight = albedo;',
      /* glsl */ `vec3 outgoingLight = mix(albedo, navrenSkyColor,
        smoothstep(60.0, 180.0, distance) * 0.035);
      vec2 contactDelta = (navrenBaseXZ - navrenContactCenter) / vec2(1.8, 0.34);
      float contactOcclusion = exp(-dot(contactDelta, contactDelta) * 1.5)
        * navrenContactStrength * 0.018;
      outgoingLight *= 1.0 - contactOcclusion;`,
      'horizon tint'
    );
    material.needsUpdate = true;
  } catch (error) {
    geometry.dispose();
    normals.dispose();
    mirrorTexture.dispose();
    material.dispose();
    throw error;
  }

  let disposed = false;
  const officialBeforeRender = mesh.onBeforeRender;
  mesh.onBeforeRender = function (renderer, scene, camera) {
    if (disposed) return;
    const originalSetRenderTarget = renderer.setRenderTarget;
    // The upstream target lives in Water's closure. Capture the actual owner
    // when Water binds it rather than assuming a property on its texture.
    renderer.setRenderTarget = function (target, ...args) {
      if (target && target.texture === mirrorTexture) {
        if (!reflectionTarget) {
          target.samples = Math.min(4, renderer.capabilities?.maxSamples || 0);
        }
        reflectionTarget = target;
        if (target.width !== desiredReflectionResolution || target.height !== desiredReflectionResolution) {
          target.setSize(desiredReflectionResolution, desiredReflectionResolution);
        }
      }
      return originalSetRenderTarget.call(this, target, ...args);
    };
    try {
      officialBeforeRender.call(mesh, renderer, scene, camera);
    } finally {
      renderer.setRenderTarget = originalSetRenderTarget;
    }
  };

  function getQuality() {
    return {
      reflectionResolution: desiredReflectionResolution,
      reflectionTargetCaptured: !!reflectionTarget,
      reflectionTargetWidth: reflectionTarget?.width || null,
      reflectionTargetHeight: reflectionTarget?.height || null,
      reflectionTargetSamples: reflectionTarget ? reflectionTarget.samples : null,
      reflectionSamples: reflectionTarget ? reflectionTarget.samples : null,
      segments,
      vertices: geometry.attributes.position.count,
      totalVertices: geometry.attributes.position.count,
      normalResolution: normals.image.width
    };
  }

  return {
    mesh,
    getQuality,
    setQuality({ reflectionResolution: nextResolution } = {}) {
      if (disposed) return getQuality();
      if (nextResolution !== undefined) {
        desiredReflectionResolution = boundedReflectionResolution(nextResolution);
        material.uniforms.navrenReflectionTexel.value.set(
          1 / desiredReflectionResolution, 1 / desiredReflectionResolution
        );
        if (reflectionTarget) {
          reflectionTarget.setSize(desiredReflectionResolution, desiredReflectionResolution);
        }
      }
      return getQuality();
    },
    update(time) {
      if (disposed || !Number.isFinite(time)) return;
      material.uniforms.time.value = time;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      mesh.removeFromParent();
      mesh.onBeforeRender = () => {};
      geometry.dispose();
      normals.dispose();
      if (reflectionTarget) reflectionTarget.dispose();
      else mirrorTexture.dispose();
      reflectionTarget = null;
      material.dispose();
    }
  };
}
