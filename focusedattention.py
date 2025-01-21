## Focused attention -- These blocks respond to relative positional bias in real-time during training. 
## The bias in turn is based on loss updates through the model via max_dist and base (frequency). You will see these updates print to screen during training. 
## You can substiute other types of attention for both local, global, anf the multihead within local if your local attention has a multihead.
## Goes with Echo

class AdaptiveSpanAttention(nn.Module):
    def __init__(self, base, n_state, n_head, max_dist, win_size, max_span, temp_scale=0.01):
        super().__init__()

        self.max_dist = max_dist
        self.win_size = win_size
        self.max_span = max_span
        self.temp_scale = temp_scale
        self.multi_attn = MultiheadAttention(base, n_state, n_head, max_dist)
        self.span_scale = nn.Parameter(torch.tensor(1.0))

    def forward(self, query, key, value, span_scale):
        span_len = int(self.max_span * span_scale.mean().item())
        span_len = min(span_len, query.shape[1], key.shape[1], value.shape[1])

        eff_span = min(span_len, self.max_dist)
        q_span = query[:, :eff_span, :]
        k_span = key[:, :eff_span, :]
        v_span = value[:, :eff_span, :]

        attn_out, attn_weights = self.multi_attn(q_span, k_span, v_span)
        temperature = 1.0 - self.temp_scale * span_scale  

        n_batch, n_ctx, n_state = q_span.shape
        scale = (n_state // self.multi_attn.n_head) ** -0.25

        q = q_span.view(*q_span.shape[:2], self.multi_attn.n_head, -1).permute(0, 2, 1, 3)
        k = k_span.view(*k_span.shape[:2], self.multi_attn.n_head, -1).permute(0, 2, 1, 3)
        v = v_span.view(*v_span.shape[:2], self.multi_attn.n_head, -1).permute(0, 2, 1, 3)

        attn_scores = torch.matmul(q, k.transpose(-2, -1))
        attn_weights = torch.softmax((attn_scores / temperature) * scale, dim=-1)
        attn_out = torch.matmul(attn_weights, v)
        attn_out = attn_out.permute(0, 2, 1, 3).flatten(start_dim=2)
        attn_weights = attn_weights * (1.0 / span_scale)     
       
        attn_out = torch.bmm(attn_weights.view(-1, *attn_weights.shape[2:]), v.view(-1, *v.shape[2:]))
        attn_out = attn_out.view(query.size(0), query.size(1), -1)
        attn_out = attn_out.permute(0, 2, 1).contiguous().view(query.size(0), -1, query.size(2))    

        return attn_out, attn_weights

class SpanPredictor(nn.Module):
    def __init__(self, n_state):
        super().__init__()
        self.linear = nn.Linear(n_state, 1)

    def forward(self, global_out):
        scale = torch.sigmoid(self.linear(global_out))
        return scale
    
class HybridAttention(nn.Module):
    def __init__(self, base, n_state, n_head, max_dist, win_size = 32, max_span = 32, slid_win = 32):
        super().__init__()
        self.max_dist = max_dist
        self.win_size = win_size
        self.max_span = max_span
        self.slid_win = slid_win

        self.span_pred = SpanPredictor(n_state)
        self.dist_local = max_dist  
        self.dist_global = max_dist
        self.attn_local = AdaptiveSpanAttention(base, n_state, n_head, self.dist_local, win_size, max_span)
        self.attn_global = MultiheadAttention(base, n_state, n_head, self.dist_global)
        self.ln_local = LayerNorm(n_state)
        self.ln_global = LayerNorm(n_state)
        self.projection = Linear(2 * n_state, n_state)

    def forward(self, x, new_dist=None, new_base=None, xa = None, mask = None, kv_cache = None):
 
        local = self.ln_local(x)
        globe= self.ln_global(x)

        globe_out, _ = self.attn_global(globe, globe, globe)
        span_scale = self.span_pred(globe_out.mean(dim=1)) 

        win_size = max(1, int(self.slid_win * span_scale.mean().item()))
        span_len = max(1, int(self.max_span * span_scale.mean().item()))

        effective_max_dist = min(self.max_dist, local.size(1))
        local_max_dist = min(self.dist_local, span_len, win_size)
        globe_max_dist = effective_max_dist
        self.attn_local.max_dist = local_max_dist
        self.attn_global.max_dist = globe_max_dist

        local_out = self.slid_win_attention(local, win_size, span_len, span_scale)
        combined = torch.cat([local_out.permute(1, 0, 2), globe_out.permute(1, 0, 2)], dim=-1)
        x = self.projection(combined)
        return x
    
    def slid_win_attention(self, x, win_size, span_len, span_scale):
        batch_size, seq_len, n_state = x.size()
        out = torch.zeros_like(x, device=x.device)

        for i in range(0, seq_len, win_size):
            end = min(i + win_size, seq_len)
            query = x[:, i:end, :]
            start = max(0, i - span_len + win_size)
            key = x[:, start:i + span_len, :]
            value = x[:, start:i + span_len, :]
 
            attn_out, _ = self.attn_local(query, key, value, span_scale)
            x[:, i:end, :] = attn_out

        return x
    
class ResidualAttentionBlock(nn.Module):

    def __init__(self, base: int, n_state: int, n_head: int, max_dist: int, hybrid_attention: bool = False, cross_attention: bool = False):
        super().__init__()
        self.cross_attention = cross_attention
        self.hybrid_attention = hybrid_attention

        if hybrid_attention:
            self.attn = HybridAttention(base, n_state, n_head, max_dist)
        else:
            self.attn = MultiheadAttention(base, n_state, n_head, max_dist)
        self.attn_ln = nn.LayerNorm(n_state)

        if cross_attention:
            if hybrid_attention:
                 self.cross_attn = HybridAttention(base, n_state, n_head, max_dist)
            else:
                self.cross_attn = MultiheadAttention(base, n_state, n_head, max_dist)
            self.cross_attn_ln = nn.LayerNorm(n_state)


        n_mlp = n_state * 4
        self.mlp = nn.Sequential(
            nn.Linear(n_state, n_mlp), nn.GELU(), nn.Linear(n_mlp, n_state)
        )
        self.mlp_ln = nn.LayerNorm(n_state)

    def forward(self, x: torch.Tensor, xa: Optional[torch.Tensor] = None, mask: Optional[torch.Tensor] = None) -> torch.Tensor:
        x = x + self.attn(self.attn_ln(x), mask=mask)[0]

        if self.cross_attention:
            x = x + self.cross_attn(self.cross_attn_ln(x), xa, mask=mask)[0]

        x = x + self.mlp(self.mlp_ln(x))
        return x
