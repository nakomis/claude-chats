// att_heads as it was before CHATS-5 (chats-4-memory-budget), verbatim but for its name: the whole
// pass is one sequence. test_att.c's reference for the single-message path.
static void att_heads_old(void *arg)
{
    att_t *a = arg;
    int n = n_tok;
    float *qb = a->scr, *acc = qb + QB * HD, *kb = acc + QB * HD, *vb = kb + KC * HD, *mx = vb + KC * HD, *l = mx + QB;
    for (int h = a->h0; h < a->h1; h++) {
        for (int q0 = 0; q0 < n; q0 += QB) {
            int nq = n - q0 < QB ? n - q0 : QB;
            for (int i = 0; i < nq; i++) {
                const uint16_t *s = a->q + (size_t)(q0 + i) * D + h * HD;
                for (int d = 0; d < HD; d++) qb[i * HD + d] = h2f(s[d]) * 0.125f;   // 1 / sqrt(HD)
                mx[i] = -1e30f; l[i] = 0;
            }
            memset(acc, 0, (size_t)nq * HD * sizeof(float));
            for (int k0 = 0; k0 < n; k0 += KC) {
                int nk = n - k0 < KC ? n - k0 : KC;
                for (int j = 0; j < nk; j++) {
                    const uint16_t *ks = a->k + (size_t)(k0 + j) * D + h * HD, *vs = a->v + (size_t)(k0 + j) * D + h * HD;
                    for (int d = 0; d < HD; d++) { kb[j * HD + d] = h2f(ks[d]); vb[j * HD + d] = h2f(vs[d]); }
                }
                for (int i = 0; i < nq; i++) {
                    const float *qi = qb + i * HD;
                    float sc[KC], m = mx[i];
                    int j = 0;
                    for (; j + 4 <= nk; j += 4) {     // four keys at once: q[d] loaded once
                        const float *k_0 = kb + j * HD, *k_1 = k_0 + HD, *k_2 = k_1 + HD, *k_3 = k_2 + HD;
                        float s0 = 0, s1 = 0, s2 = 0, s3 = 0;
                        for (int d = 0; d < HD; d++) {
                            float qd = qi[d];
                            s0 += qd * k_0[d]; s1 += qd * k_1[d]; s2 += qd * k_2[d]; s3 += qd * k_3[d];
                        }
                        sc[j] = s0; sc[j + 1] = s1; sc[j + 2] = s2; sc[j + 3] = s3;
                    }
                    for (; j < nk; j++) {
                        const float *kj = kb + j * HD;
                        float s = 0;
                        for (int d = 0; d < HD; d++) s += qi[d] * kj[d];
                        sc[j] = s;
                    }
                    for (j = 0; j < nk; j++) if (sc[j] > m) m = sc[j];
                    float corr = fast_exp(mx[i] - m), sum = 0;
                    for (j = 0; j < nk; j++) { sc[j] = fast_exp(sc[j] - m); sum += sc[j]; }
                    mx[i] = m;
                    l[i] = l[i] * corr + sum;
                    float *ai = acc + i * HD;
                    for (int d = 0; d < HD; d += 8) {          // eight output dims held in registers
                        float a0 = ai[d] * corr, a1 = ai[d + 1] * corr, a2 = ai[d + 2] * corr, a3 = ai[d + 3] * corr;
                        float a4 = ai[d + 4] * corr, a5 = ai[d + 5] * corr, a6 = ai[d + 6] * corr, a7 = ai[d + 7] * corr;
                        const float *vp = vb + d;
                        for (j = 0; j < nk; j++, vp += HD) {
                            float w = sc[j];
                            a0 += w * vp[0]; a1 += w * vp[1]; a2 += w * vp[2]; a3 += w * vp[3];
                            a4 += w * vp[4]; a5 += w * vp[5]; a6 += w * vp[6]; a7 += w * vp[7];
                        }
                        ai[d] = a0; ai[d + 1] = a1; ai[d + 2] = a2; ai[d + 3] = a3;
                        ai[d + 4] = a4; ai[d + 5] = a5; ai[d + 6] = a6; ai[d + 7] = a7;
                    }
                }
            }
            for (int i = 0; i < nq; i++) {
                uint16_t *o = a->q + (size_t)(q0 + i) * D + h * HD;
                float inv = 1.0f / l[i];
                for (int d = 0; d < HD; d++) o[d] = f2h(acc[i * HD + d] * inv);
            }
        }
    }
}
