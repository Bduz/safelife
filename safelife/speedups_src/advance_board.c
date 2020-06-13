#include <string.h>
#include "advance_board.h"
#include "constants.h"
#include "random.h"

static const uint16_t ALIVE_BITS = (1 << 4) - 1;
static const uint16_t DESTRUCTIBLE2 = 1 << 8;
static const uint16_t FLAGS1 = PRESERVING | INHIBITING | SPAWNING;
static const uint16_t FLAGS2 = (1 << 8) | COLORS;


static void combine_neighbors(uint16_t src, uint16_t *dst) {
    // Combine neighbors with base board.
    uint16_t alive = src & ALIVE;
    uint16_t src_flags1 = src & FLAGS1;
    uint16_t src_flags2 = (src & FLAGS2) * alive;
    uint16_t dst_flags2 = *dst & FLAGS2;
    *dst |= (dst_flags2 & src_flags2) << 4;
    *dst |= ((src & COLORS) << 4) * ((src & SPAWNING) > 0);
    *dst |= src_flags1;
    *dst |= src_flags2;
    *dst += alive;
}


static void combine_neighbors2(uint16_t src, uint16_t *dst) {
    // Combine combinations.
    *dst |= (*dst & src & FLAGS2) << 4;
    *dst |= src & (FLAGS1 | FLAGS2 | (FLAGS2 << 4));
    *dst += src & ALIVE_BITS;

}

void advance_board(
        uint16_t *b1, uint16_t *b2, int nrow, int ncol, float spawn_prob) {
    int size = nrow*ncol;
    int i, j, start_of_row, end_of_row, end_of_col;
    uint16_t c1[size];
    memset(c1, 0, sizeof(c1));

    // Adjust all of the bits in b2 so that the destructible bit overwrites
    // the exit bit. This allows us to treat destructibility and colors at
    // the same time.
    for (i = 0; i < size; i++) {
        b2[i] = b1[i] | (b1[i] & DESTRUCTIBLE) << 5;
    }

    // First figure out what the neighboring bits are.
    // Can do this in two 1-d convolutions.

    // Combine along rows
    for (i = 0; i < nrow; i++) {
        // Wrap at start of row
        start_of_row = i * ncol;
        end_of_row = start_of_row + ncol - 1;
        combine_neighbors(b2[start_of_row], c1+start_of_row);
        combine_neighbors(b2[start_of_row+1], c1+start_of_row);
        combine_neighbors(b2[end_of_row], c1+start_of_row);
        // Loop over middle
        for (j = start_of_row + 1; j < end_of_row; j++) {
            combine_neighbors(b2[j], c1+j);
            combine_neighbors(b2[j+1], c1+j);
            combine_neighbors(b2[j-1], c1+j);
        }
        // Wrap at end of row
        combine_neighbors(b2[end_of_row], c1+end_of_row);
        combine_neighbors(b2[end_of_row-1], c1+end_of_row);
        combine_neighbors(b2[start_of_row], c1+end_of_row);
    }

    // Combine along columns
    // Store the combined values in b2.
    for (i = 0; i < ncol; i++) {
        end_of_col = i + (nrow-1)*ncol;
        b2[i] = c1[i];
        combine_neighbors2(c1[i+ncol], b2+i);
        combine_neighbors2(c1[end_of_col], b2+i);
        for (j = i+ncol; j < end_of_col; j += ncol) {
            b2[j] = c1[j];
            combine_neighbors2(c1[j-ncol], b2+j);
            combine_neighbors2(c1[j+ncol], b2+j);
        }
        b2[end_of_col] = c1[end_of_col];
        combine_neighbors2(c1[i], b2 + end_of_col);
        combine_neighbors2(c1[end_of_col-ncol], b2 + end_of_col);
    }

    // Now loop over the board and advance it.
    for (i = 0; i < size; i++) {
        uint16_t num_alive = b2[i] & ALIVE_BITS;
        if (b1[i] & ALIVE) {
            // Note that if it's alive, it counts as its own neighbor.
            if (b1[i] & FROZEN || b2[i] & PRESERVING ||
                num_alive == 3 || num_alive == 4) {
                // copy the old cell
                b2[i] = b1[i];
            } else {
                // kill the cell
                b2[i] = 0;
            }
        } else {  // starts dead
            if (b1[i] & FROZEN || b2[i] & INHIBITING) {
                // copy the old cell
                b2[i] = b1[i];
            } else if (num_alive == 3) {
                // add a new live cell
                b2[i] = ALIVE |
                    ((b2[i] & (COLORS << 4)) >> 4) |
                    ((b2[i] & (DESTRUCTIBLE2 << 4)) >> 9);
            } else if (b2[i] & SPAWNING && random_float() < spawn_prob) {
                // add a spawned cell
                b2[i] = ALIVE | DESTRUCTIBLE |
                    ((b2[i] & (COLORS << 4)) >> 4);
            } else {
                // copy the old cell
                b2[i] = b1[i];
            }
        }
    }
}


void alive_counts(uint16_t *board, uint16_t *goals, int n, int64_t *out) {
    uint16_t b, g, k;
    for (int i=0; i<n; i++) {
        b = board[i];
        g = goals[i];
        k = ((b & COLORS) >> COLOR_BIT) | ((g & COLORS) >> (COLOR_BIT - 3));
        out[k] += b & ALIVE;
    }
}


static int clip(int x, int width) {
    while (x < 0) x += width;
    while (x >= width) x -= width;
    return x;
}


void execute_actions(
        uint16_t *board, int w, int h,
        int64_t *locations, int64_t *actions, int n_agents, int action_stride) {
    for (int k=0; k < n_agents; k++) {
        int64_t action = *actions;
        actions += action_stride;
        if (action == 0) continue;

        int direction = (action - 1) & 3;
        int dx, dy;
        if (direction & 1) {
            dx = 2 - direction;
            dy = 0;
        } else {
            dx = 0;
            dy = direction - 1;
        }
        int y0 = locations[2*k] % h;
        int x0 = locations[2*k+1] % w;
        uint16_t *p0 = board + (x0 + y0*w);
        uint16_t *p1 = board + (clip(x0+dx, w) + clip(y0+dy, h)*w);
        uint16_t *p2 = board + (clip(x0+2*dx, w) + clip(y0+2*dy, h)*w);
        uint16_t *p3 = board + (clip(x0-dx, w) + clip(y0-dy, h)*w);

        if (!(*p0 & AGENT)) continue;

        // Re-orient the agent.
        *p0 &= ~ORIENTATION_MASK;
        *p0 |= direction << ORIENTATION_BIT;


        if(action >= 5) {  // toggle action
            if (!(*p1)) {  // empty block. Add a life cell.
                *p1 = ALIVE | DESTRUCTIBLE | (*p0 & COLORS);
            } else if (*p1 & DESTRUCTIBLE) {
                *p1 = 0;
            } else if (~*p0 & *p1 & PUSHABLE) {
                // "shove" the block without moving
                if (!(*p2)) {
                    *p2 = *p1;
                    *p1 = 0;
                } else if (*p2 & EXIT) {
                    // Push the block out the exit
                    *p1 = 0;
                }
            }
        } else {  // move action
            if (~*p0 & *p1 & PUSHABLE) {
                // Agent can only push pushable blocks, but only if it
                // is not itself pushable.
                if (!(*p2)) {
                    *p2 = *p1;
                    goto move;
                } else if (*p2 & EXIT) {
                    // Push the block out the exit
                    goto move;
                }
            } else if (!(*p1)) {
                goto move;
            } else if (*p1 & EXIT && *p1 & COLORS) {
                goto exit_move;
            }
            continue;

            move:
            *p1 = *p0;
            exit_move:
            locations[2*k] = clip(y0 + dy, h);
            locations[2*k+1] = clip(x0 + dx, w);
            if (~*p0 & *p3 & PULLABLE) {
                *p0 = *p3;
                *p3 = 0;
            } else {
                *p0 = 0;
            }
        }
    }
}
