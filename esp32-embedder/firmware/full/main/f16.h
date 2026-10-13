// fp16 <-> fp32 without hardware support (after Fabian Giesen's public-domain
// float_to_half_fast3_rtne / half_to_float): round to nearest even, subnormals kept
static inline uint16_t f2h(float ff)
{
    union { uint32_t u; float f; } f = { .f = ff }, dm = { .u = ((127u - 15u) + (23u - 10u) + 1u) << 23 };
    uint32_t sign = f.u & 0x80000000u;
    uint16_t o;
    f.u ^= sign;
    if (f.u >= (127u + 16u) << 23) o = f.u > 255u << 23 ? 0x7e00 : 0x7c00;        // NaN / overflow to inf
    else if (f.u < 113u << 23) { f.f += dm.f; o = (uint16_t)(f.u - dm.u); }        // subnormal or zero
    else {
        uint32_t odd = (f.u >> 13) & 1;
        f.u += ((uint32_t)(15 - 127) << 23) + 0xfff + odd;
        o = (uint16_t)(f.u >> 13);
    }
    return o | (uint16_t)(sign >> 16);
}
static inline float h2f(uint16_t h)
{
    union { uint32_t u; float f; } o = { .u = (uint32_t)(h & 0x7fffu) << 13 }, magic = { .u = 113u << 23 };
    uint32_t exp = o.u & 0x0f800000u;
    o.u += (127u - 15u) << 23;
    if (exp == 0x0f800000u) o.u += (128u - 16u) << 23;                              // inf / NaN
    else if (exp == 0) { o.u += 1u << 23; o.f -= magic.f; }                          // subnormal
    o.u |= (uint32_t)(h & 0x8000u) << 16;
    return o.f;
}
