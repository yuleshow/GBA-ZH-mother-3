.syntax unified
.cpu arm7tdmi
.thumb
.section .text
.global enemy_one_hit
.thumb_func
enemy_one_hit:
    push {r4, r5, lr}
    adds r5, r0, #0
    adds r4, r1, #0
    bl 0x080726b8
    lsls r0, r0, #24
    beq enemy_damage
    cmp r4, #0
    bgt apply_hp
    movs r4, #1
    b apply_hp
enemy_damage:
    ldr r0, [r5, #0x2c]
    cmp r4, r0
    bge apply_hp
    movs r4, #0
apply_hp:
    adds r0, r5, #0
    adds r1, r4, #0
    bl 0x080750f0
    pop {r4, r5}
    pop {r0}
    bx r0
    .balign 4, 0
