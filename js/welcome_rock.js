// The logo and the coasts share their meshes. Only cloned physical materials
// are owned here; the voyage continues to own every position and morph target.
const ROCK_SHADER_KEY = 'navren-photographic-coasts-v5';

const ROCK_FRAGMENT = /* glsl */ `
uniform float navrenRockBlend;
uniform float navrenRockSeed;
uniform float navrenRockReflection;
uniform vec3 navrenRockDark;
uniform vec3 navrenRockBlue;
uniform vec3 navrenRockPale;
uniform float navrenRockPhotoReady;
uniform sampler2D navrenRockPhotoColor;
uniform sampler2D navrenRockPhotoHeight;
uniform sampler2D navrenRockPhotoArm;
varying vec3 vNavrenRockWorld;
varying vec3 vNavrenRockWorldNormal;

vec4 navrenRockTriplanar(sampler2D surfaceMap, vec3 p, vec3 weights) {
  // A full photograph spans 5.5 world units. Repeat wrapping and mipmaps keep
  // nearby rock detailed and the distant coast quiet without UV stretching.
  vec3 coordinates = p / 5.5;
  // Derivatives are evaluated before the direction-dependent branch. Explicit
  // gradients keep mip levels stable across neighboring dominant/mixed faces.
  vec3 dx = dFdx(coordinates);
  vec3 dy = dFdy(coordinates);
  if (weights.x > 0.98) return textureGrad(surfaceMap, coordinates.zy, dx.zy, dy.zy);
  if (weights.y > 0.98) return textureGrad(surfaceMap, coordinates.xz, dx.xz, dy.xz);
  if (weights.z > 0.98) return textureGrad(surfaceMap, coordinates.xy, dx.xy, dy.xy);
  return textureGrad(surfaceMap, coordinates.zy, dx.zy, dy.zy) * weights.x
    + textureGrad(surfaceMap, coordinates.xz, dx.xz, dy.xz) * weights.y
    + textureGrad(surfaceMap, coordinates.xy, dx.xy, dy.xy) * weights.z;
}

float navrenRockHash(vec3 p) {
  p = fract(p * 0.1031);
  p += dot(p, p.yzx + 33.33);
  return fract((p.x + p.y) * p.z);
}

float navrenRockNoise(vec3 p) {
  vec3 cell = floor(p);
  vec3 f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(mix(navrenRockHash(cell), navrenRockHash(cell + vec3(1.0, 0.0, 0.0)), f.x),
        mix(navrenRockHash(cell + vec3(0.0, 1.0, 0.0)),
            navrenRockHash(cell + vec3(1.0, 1.0, 0.0)), f.x), f.y),
    mix(mix(navrenRockHash(cell + vec3(0.0, 0.0, 1.0)),
            navrenRockHash(cell + vec3(1.0, 0.0, 1.0)), f.x),
        mix(navrenRockHash(cell + vec3(0.0, 1.0, 1.0)),
            navrenRockHash(cell + vec3(1.0, 1.0, 1.0)), f.x), f.y), f.z);
}

float navrenRockFbm(vec3 p) {
  float value = navrenRockNoise(p) * 0.57;
  p = p * 2.03 + vec3(8.3, 17.1, 4.7);
  value += navrenRockNoise(p) * 0.28;
  p = p * 2.07 + vec3(13.7, 3.1, 9.2);
  return value + navrenRockNoise(p) * 0.15;
}

float navrenRockCrack(float distanceToCrack, float coordinate) {
  float footprint = max(fwidth(coordinate), 0.003);
  float resolved = 1.0 - smoothstep(0.10, 0.38, footprint);
  return (1.0 - smoothstep(0.008, 0.022 + footprint, distanceToCrack)) * resolved;
}

struct NavrenRockSurface {
  vec3 color;
  float height;
  float roughness;
};

vec3 navrenRockPhotographicColor(vec3 photograph) {
  float luminance = max(dot(photograph, vec3(0.2126, 0.7152, 0.0722)), 0.008);
  float tone = clamp(pow(luminance * 4.2, 0.72), 0.10, 0.82);
  vec3 stone = mix(navrenRockDark, navrenRockPale, tone);
  return stone * mix(vec3(1.0), clamp(photograph / luminance,
    vec3(0.65), vec3(1.35)), 0.12);
}

NavrenRockSurface navrenRockReflected(vec3 p, vec3 worldPosition, vec3 worldNormal) {
  NavrenRockSurface stone;
  stone.color = navrenRockBlue;
  stone.height = 0.0;
  stone.roughness = 0.90;
  if (navrenRockPhotoReady > 0.5) {
    vec3 weights = abs(normalize(worldNormal));
    weights = weights * weights * weights * weights * weights;
    weights /= max(weights.x + weights.y + weights.z, 0.0001);
    // The distorted river reflection needs rock color, not the displacement,
    // ARM maps or broad/noisy surface calculations used by the real coast.
    vec3 photograph = navrenRockTriplanar(navrenRockPhotoColor, p, weights).rgb;
    float wet = 1.0 - smoothstep(0.05, 1.3, worldPosition.y);
    stone.color = navrenRockPhotographicColor(photograph) * 0.92 * (1.0 - wet * 0.10);
  }
  return stone;
}

NavrenRockSurface navrenRockPhotograph(vec3 p, vec3 worldPosition, vec3 worldNormal) {
  vec3 weights = abs(normalize(worldNormal));
  weights = weights * weights * weights * weights * weights;
  weights /= max(weights.x + weights.y + weights.z, 0.0001);
  vec3 photograph = navrenRockTriplanar(navrenRockPhotoColor, p, weights).rgb;
  vec3 arm = navrenRockTriplanar(navrenRockPhotoArm, p, weights).rgb;
  float relief = navrenRockTriplanar(navrenRockPhotoHeight, p, weights).r;
  // Photography already contains fine strata, fractures, pores and grain.
  // Only two inexpensive broad fields contribute the larger geological form.
  float block = navrenRockNoise(p * vec3(0.37, 0.23, 0.37) + 5.3);
  float flake = navrenRockNoise(p * vec3(0.91, 0.34, 0.91) + 17.2);
  float wet = 1.0 - smoothstep(0.05, 1.3, worldPosition.y);
  vec3 photographicStone = navrenRockPhotographicColor(photograph);
  photographicStone *= (0.83 + arm.r * 0.17) * (1.0 - wet * 0.10);
  NavrenRockSurface stone;
  vec3 largeFaceColor = mix(navrenRockDark, navrenRockBlue,
    0.21 + smoothstep(0.22, 0.76, block * 0.71 + flake * 0.29) * 0.72);
  stone.color = mix(largeFaceColor, photographicStone, 0.88);
  stone.height = (block - 0.5) * 0.52 + (flake - 0.5) * 0.13 + (relief - 0.5) * 0.32;
  stone.roughness = clamp(arm.g * 0.86 + 0.16 - wet * 0.05, 0.83, 0.99);
  return stone;
}

NavrenRockSurface navrenRockSurface(vec3 worldPosition, vec3 worldNormal) {
  // No view coordinates or time enter the geological field. Moving the
  // camera (including the water reflection camera) cannot move the texture.
  vec3 p = worldPosition + vec3(navrenRockSeed * 9.7, 0.0, navrenRockSeed * 13.1);
  if (navrenRockReflection > 0.5) return navrenRockReflected(p, worldPosition, worldNormal);
  // Uniform across the draw: once loaded, skip every detailed procedural
  // layer/crack/erosion calculation instead of discarding 88% of its result.
  if (navrenRockPhotoReady > 0.5) return navrenRockPhotograph(p, worldPosition, worldNormal);
  float weather = navrenRockFbm(p * vec3(0.13, 0.095, 0.13));
  vec3 warped = p + vec3((weather - 0.5) * 1.4, 0.0, (weather - 0.5) * 0.9);
  float block = navrenRockNoise(warped * vec3(0.37, 0.23, 0.37) + 5.3);
  float flake = navrenRockNoise(warped * vec3(0.91, 0.34, 0.91) + 17.2);
  float chip = navrenRockNoise(warped * vec3(1.74, 0.97, 1.74) + 27.1);
  float grain = navrenRockNoise(warped * 7.3 + 31.4);

  // Non-periodic deposits have varying thickness and eroded gaps. The beds
  // read through broad dark material changes, not bright raised contour lines.
  float bedDepth = p.y * 0.61 + (weather - 0.5) * 0.91 + p.z * 0.019;
  float deposit = navrenRockNoise(vec3(bedDepth, 19.4 + navrenRockSeed, 3.2));
  float bedBreak = smoothstep(0.24, 0.65, flake * 0.6 + block * 0.4);
  float darkBed = smoothstep(0.51, 0.67, deposit) * bedBreak;
  float bedEdge = (1.0 - smoothstep(0.015, 0.060 + fwidth(deposit), abs(deposit - 0.54))) * bedBreak;

  // Each longitudinal sector has a differently positioned fracture. The
  // crack is an open, broken line with a local fork; it never outlines cells.
  float crackCoordinate = p.z * 0.235 + p.x * 0.14;
  float sector = floor(crackCoordinate);
  float crackCentre = mix(0.20, 0.80, navrenRockHash(vec3(sector, navrenRockSeed, 7.3)));
  float wobble = (flake - 0.5) * 0.17 + (chip - 0.5) * 0.045;
  float crackDistance = abs(fract(crackCoordinate) - crackCentre + wobble);
  float crackLength = navrenRockNoise(vec3(sector * 1.71, p.y * 0.21, navrenRockSeed + 41.2));
  float crack = navrenRockCrack(crackDistance, crackCoordinate) * smoothstep(0.23, 0.47, crackLength);
  float forkHeight = mix(3.0, 20.0, navrenRockHash(vec3(sector, 18.9, navrenRockSeed)));
  float forkY = p.y - forkHeight;
  float forkDistance = abs(fract(crackCoordinate) - crackCentre + wobble - clamp(forkY * 0.027, 0.0, 0.22));
  float fork = navrenRockCrack(forkDistance, crackCoordinate)
    * smoothstep(0.0, 1.4, forkY) * (1.0 - smoothstep(5.0, 8.0, forkY));
  float joints = max(crack, fork * 0.70);
  float pores = smoothstep(0.64, 0.86, chip);
  float pixelWorld = max(length(dFdx(p)), length(dFdy(p)));
  float flakeResolved = 1.0 - smoothstep(0.22, 0.90, pixelWorld);
  float chipResolved = 1.0 - smoothstep(0.11, 0.45, pixelWorld);
  float grainResolved = 1.0 - smoothstep(0.05, 0.21, pixelWorld);
  float wet = 1.0 - smoothstep(0.05, 1.3, worldPosition.y);

  NavrenRockSurface stone;
  float faceTone = smoothstep(0.22, 0.76, block * 0.71 + flake * 0.29);
  stone.color = mix(navrenRockDark, navrenRockBlue, 0.21 + faceTone * 0.72);
  stone.color = mix(stone.color, navrenRockPale,
    smoothstep(0.55, 0.82, flake) * 0.34 + smoothstep(0.69, 0.88, block) * 0.17);
  stone.color *= 1.0 - darkBed * 0.25 - joints * 0.31 - pores * 0.10 * chipResolved;
  stone.color *= 1.0 + (grain - 0.5) * 0.13 * grainResolved;
  stone.color *= 1.0 - wet * 0.12;
  // Macro relief carries the appearance: fractured blocks, flattened flakes
  // and chipped faces. Dark bed/crack color does not create a sharp white rim.
  float flakeRidge = abs(flake - 0.48);
  stone.height = (block - 0.5) * 1.08 + (weather - 0.5) * 0.48
    + (flake - 0.5) * 0.31 * flakeResolved - flakeRidge * 0.27 * flakeResolved
    + (chip - 0.5) * 0.075 * chipResolved - pores * 0.035 * chipResolved
    - darkBed * 0.025 - bedEdge * 0.005 - joints * 0.006
    + (grain - 0.5) * 0.010 * grainResolved;
  stone.roughness = clamp(0.91 + (weather - 0.5) * 0.10 + pores * 0.04
    - wet * 0.06, 0.83, 0.99);
  return stone;
}

vec3 navrenRockRelief(vec3 viewPosition, vec3 surfaceNormal, float height, float face) {
  vec3 sx = dFdx(viewPosition);
  vec3 sy = dFdy(viewPosition);
  vec3 r1 = cross(sy, surfaceNormal);
  vec3 r2 = cross(surfaceNormal, sx);
  float determinant = dot(sx, r1) * face;
  vec2 heightGradient = vec2(dFdx(height), dFdy(height));
  if (abs(determinant) < 1.0e-10) return surfaceNormal;
  vec3 gradient = sign(determinant) * (heightGradient.x * r1 + heightGradient.y * r2);
  vec3 slope = gradient / abs(determinant);
  // Extreme near-camera height gradients must not create white edge ribbons.
  slope *= min(1.0, 0.95 / max(length(slope), 0.0001));
  return normalize(surfaceNormal - slope);
}
`;

function inject(source, anchor, addition) {
  if (!source.includes(anchor)) throw new Error(`Navren rock shader is missing ${anchor}`);
  return source.replace(anchor, `${anchor}\n${addition}`);
}

/**
 * update(progress, elapsed): progress is the whole 6.8-second voyage in [0, 1].
 * Pass 0 before launch. Geological relief appears during seconds 0.54–1.77.
 * elapsed is accepted for the scene API; the rock pattern is intentionally static.
 * ready resolves to true once all three local photographic maps are usable.
 * It is optional: the logo and procedural fallback never wait for image I/O.
 * Optional { mainCamera } enables a color-only coast in other render cameras.
 * Without it every camera retains the complete physical rock surface.
 * dispose() restores the original materials, releases only this module's maps,
 * and preserves the original shared logo textures.
 */
export function createRockCoasts(THREE, brand, { mainCamera = null } = {}) {
  const meshes = brand.children.filter((mesh) => mesh.isMesh && mesh.material?.isMeshPhysicalMaterial);
  const blend = { value: 0 };
  const palette = {
    // Neutral weathered sandstone; the scene's cool ambient light and blue
    // river reflection supply the blue shadows instead of dyeing every face.
    navrenRockDark: { value: new THREE.Color(0x676660) },
    navrenRockBlue: { value: new THREE.Color(0x908b7d) },
    navrenRockPale: { value: new THREE.Color(0xb0a694) }
  };
  let disposed = false;
  const textureResources = new Set();
  function fallbackTexture(bytes, colorSpace) {
    const texture = new THREE.DataTexture(new Uint8Array(bytes), 1, 1, THREE.RGBAFormat);
    texture.colorSpace = colorSpace;
    texture.needsUpdate = true;
    textureResources.add(texture);
    return texture;
  }
  const photoUniforms = {
    navrenRockPhotoReady: { value: 0 },
    navrenRockPhotoColor: { value: fallbackTexture([128, 120, 108, 255], THREE.SRGBColorSpace) },
    navrenRockPhotoHeight: { value: fallbackTexture([128, 128, 128, 255], THREE.NoColorSpace) },
    navrenRockPhotoArm: { value: fallbackTexture([255, 230, 0, 255], THREE.NoColorSpace) }
  };
  const loader = new THREE.TextureLoader();
  const textureDefinitions = [
    ['navrenRockPhotoColor', '/assets/brand/rock-face-diff-2k.jpg', THREE.SRGBColorSpace],
    ['navrenRockPhotoHeight', '/assets/brand/rock-face-disp-2k.jpg', THREE.NoColorSpace],
    ['navrenRockPhotoArm', '/assets/brand/rock-face-arm-2k.jpg', THREE.NoColorSpace]
  ];
  const ready = Promise.all(textureDefinitions.map(async ([uniform, path, colorSpace]) => {
    try {
      const texture = await loader.loadAsync(path);
      if (disposed) { texture.dispose(); return null; }
      texture.colorSpace = colorSpace;
      texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
      texture.magFilter = THREE.LinearFilter;
      texture.minFilter = THREE.LinearMipmapLinearFilter;
      texture.generateMipmaps = true;
      texture.anisotropy = 4;
      texture.needsUpdate = true;
      textureResources.add(texture);
      return { uniform, texture };
    } catch {
      // A failed local image leaves the procedural rock working; it must not
      // reject an unawaited promise or interrupt authentication/the logo.
      return null;
    }
  })).then((textures) => {
    if (disposed || textures.some((texture) => !texture)) return false;
    textures.forEach(({ uniform, texture }) => { photoUniforms[uniform].value = texture; });
    photoUniforms.navrenRockPhotoReady.value = 1;
    return true;
  });
  const owned = meshes.map((mesh, layer) => {
    const original = mesh.material;
    const material = original.clone();
    const originalBeforeRender = mesh.onBeforeRender;
    const originalCompile = original.onBeforeCompile;
    const originalCacheKey = original.customProgramCacheKey.call(original);
    const uniforms = {
      navrenRockBlend: blend,
      navrenRockSeed: { value: layer * 1.71 },
      navrenRockReflection: { value: 0 },
      ...palette,
      ...photoUniforms
    };
    material.name = `${original.name || mesh.name || 'Navren coast'} / stratified rock`;
    material.onBeforeCompile = function (shader, renderer) {
      originalCompile.call(this, shader, renderer);
      Object.assign(shader.uniforms, uniforms);
      shader.vertexShader = inject(shader.vertexShader, '#include <common>',
        'varying vec3 vNavrenRockWorld;\nvarying vec3 vNavrenRockWorldNormal;');
      // transformed is the current position after all morph/skinning stages.
      // This varying is independent of Three's optional vWorldPosition defines.
      shader.vertexShader = inject(shader.vertexShader, '#include <worldpos_vertex>',
        'vNavrenRockWorld = (modelMatrix * vec4(transformed, 1.0)).xyz;\n'
        + 'vNavrenRockWorldNormal = inverseTransformDirection(transformedNormal, viewMatrix);');
      shader.fragmentShader = inject(shader.fragmentShader, '#include <common>', ROCK_FRAGMENT);
      shader.fragmentShader = inject(shader.fragmentShader, '#include <color_fragment>', `
        NavrenRockSurface navrenStone;
        navrenStone.color = diffuseColor.rgb;
        navrenStone.height = 0.0;
        navrenStone.roughness = roughness;
        if (navrenRockBlend > 0.0001) navrenStone = navrenRockSurface(vNavrenRockWorld, vNavrenRockWorldNormal);
        diffuseColor.rgb = mix(diffuseColor.rgb, navrenStone.color, navrenRockBlend);
      `);
      shader.fragmentShader = inject(shader.fragmentShader, '#include <roughnessmap_fragment>',
        'roughnessFactor = mix(roughnessFactor, navrenStone.roughness, navrenRockBlend);');
      shader.fragmentShader = inject(shader.fragmentShader, '#include <metalnessmap_fragment>',
        'metalnessFactor *= 1.0 - navrenRockBlend;');
      shader.fragmentShader = inject(shader.fragmentShader, '#include <normal_fragment_maps>', `
        if (navrenRockBlend > 0.0001 && navrenRockReflection < 0.5) {
          normal = navrenRockRelief(-vViewPosition, normal,
            navrenStone.height * navrenRockBlend, faceDirection);
        }
      `);
      shader.fragmentShader = inject(shader.fragmentShader, '#include <lights_physical_fragment>', `
        #ifdef USE_CLEARCOAT
          material.clearcoat = mix(material.clearcoat, 0.004, navrenRockBlend);
          material.clearcoatRoughness = mix(material.clearcoatRoughness, 0.94, navrenRockBlend);
        #endif
        material.specularColor *= 1.0 - 0.38 * navrenRockBlend;
      `);
      // A small linear ambient floor keeps shaded blue stone readable. Direct
      // and environment light still determine its shape and most of its color.
      shader.fragmentShader = inject(shader.fragmentShader, '#include <emissivemap_fragment>',
        'totalEmissiveRadiance += navrenStone.color * (0.018 * navrenRockBlend);');
    };
    material.customProgramCacheKey = () => `${originalCacheKey}|${ROCK_SHADER_KEY}`;
    mesh.material = material;
    let beforeRender = null;
    if (mainCamera) {
      beforeRender = function (renderer, scene, renderCamera, geometry, renderMaterial, group) {
        originalBeforeRender.call(this, renderer, scene, renderCamera, geometry, renderMaterial, group);
        if (!disposed) uniforms.navrenRockReflection.value = renderCamera === mainCamera ? 0 : 1;
        // WebGLRenderer calls this before setProgram. In the pinned Three
        // version a camera change forces refreshMaterial and uniform upload;
        // each coast also has its own cloned material and independent flag.
        // No material.version/defines or ShaderMaterial-only flags are needed.
      };
      mesh.onBeforeRender = beforeRender;
    }
    return { mesh, original, material, environment: original.envMapIntensity,
      originalBeforeRender, beforeRender };
  });

  function update(progress, elapsed) {
    if (disposed) return 0;
    const value = Number.isFinite(progress) ? progress : 0;
    const t = THREE.MathUtils.clamp((value - 0.08) / 0.18, 0, 1);
    blend.value = t * t * (3 - 2 * t);
    // These are existing numeric uniforms. Shader defines, textures, geometry,
    // material versions and cache keys do not change while the voyage runs.
    owned.forEach(({ material, environment }) => {
      material.envMapIntensity = environment * (1 - blend.value * 0.32);
    });
    return blend.value;
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    owned.forEach(({ mesh, original, material, originalBeforeRender, beforeRender }) => {
      if (mesh.material === material) mesh.material = original;
      if (beforeRender && mesh.onBeforeRender === beforeRender) mesh.onBeforeRender = originalBeforeRender;
      material.dispose();
    });
    textureResources.forEach((texture) => texture.dispose());
    textureResources.clear();
  }

  return { update, dispose, ready };
}
