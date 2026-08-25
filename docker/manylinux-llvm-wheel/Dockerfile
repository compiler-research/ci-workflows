# The wheel-building toolchain image: the pypa manylinux_2_28 base
# with the llvm-wheel recipe artifact baked at /opt/llvm, so consumer
# cibuildwheel builds point manylinux-x86_64-image here and need no
# per-build provisioning. The artifact of record stays the recipe
# tarball in the Releases cache; this image is only its delivery form.
# Build context: a staging directory whose install/ is the recipe
# artifact (see .github/workflows/wheel-image.yml).
# The base must match _MANYLINUX_IMAGE in recipes/llvm-wheel/build.py.
FROM quay.io/pypa/manylinux_2_28_x86_64

COPY install/ /opt/llvm/
ENV PATH=/opt/llvm/bin:$PATH
