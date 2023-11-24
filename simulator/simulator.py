"""
simulator package
"""
import itertools
import z3
from .painter import SchedulingPainter


class Simulator:
    """Simulator"""

    def __init__(self, config: dict) -> None:
        self._pp_size = config["pp_size"]
        self._num_microbatches = config["num_microbatches"]
        self._max_activation_times = config["max_activation_times"]

        self._forward_length = config["forward_execution_time"]
        self._backward_length = config["backward_execution_time"]
        self._sequential_order_constraint_strategy = config[
            "sequential_order_constraint_strategy"
        ]

        assert isinstance(
            self._forward_length, int
        ), "forward_execution_time must be int"
        assert isinstance(
            self._backward_length, int
        ), "backward_execution_time must be int"

        assert self._sequential_order_constraint_strategy in (
            "strict",
            "double_interleaving",
            "full_interleaving",
        ), "sequential order constraint strategy is not supported"

        self._solver = z3.Optimize()
        self._forward_offsets = [[] for i in range(self._pp_size)]
        self._backward_offsets = [[] for i in range(self._pp_size)]

    def _sequential_order_constraint_strict(self):
        for mb in range(self._num_microbatches):
            # forward stages sequential constraint
            for i in range(1, self._pp_size):
                self._solver.add(
                    self._forward_offsets[i][mb]
                    >= self._forward_offsets[i - 1][mb] + self._forward_length
                )
            # backward stages sequential constraint
            for i in range(self._pp_size - 1, 0, -1):
                self._solver.add(
                    self._backward_offsets[i - 1][mb]
                    >= self._backward_offsets[i][mb] + self._backward_length
                )
            # forward-backward connection sequential constraint
            self._solver.add(
                self._backward_offsets[self._pp_size - 1][mb]
                >= self._forward_offsets[self._pp_size - 1][mb] + self._forward_length
            )

    def _sequential_order_constraint_double_interleaving(self):
        for mb in range(self._num_microbatches):
            # down pipe
            down_case = z3.And(
                *[
                    self._forward_offsets[i][mb]
                    >= self._forward_offsets[i - 1][mb] + self._forward_length
                    for i in range(1, self._pp_size)
                ],
                *[
                    self._backward_offsets[i - 1][mb]
                    >= self._backward_offsets[i][mb] + self._backward_length
                    for i in range(self._pp_size - 1, 0, -1)
                ],
                self._backward_offsets[self._pp_size - 1][mb]
                >= self._forward_offsets[self._pp_size - 1][mb] + self._forward_length,
            )
            # up pipe
            up_case = z3.And(
                *[
                    self._forward_offsets[i - 1][mb]
                    >= self._forward_offsets[i][mb] + self._forward_length
                    for i in range(self._pp_size - 1, 0, -1)
                ],
                *[
                    self._backward_offsets[i][mb]
                    >= self._backward_offsets[i - 1][mb] + self._backward_length
                    for i in range(1, self._pp_size)
                ],
                self._backward_offsets[0][mb]
                >= self._forward_offsets[0][mb] + self._forward_length,
            )

            self._solver.add(z3.Or(down_case, up_case))

    def _sequential_order_constraint_full_interleaving(self):
        for mb in range(self._num_microbatches):
            cases = []

            for perm in itertools.permutations(range(self._pp_size)):
                cases.append(
                    z3.And(
                        # forward sequential order
                        *[
                            self._forward_offsets[perm[i + 1]][mb]
                            >= self._forward_offsets[perm[i]][mb] + self._forward_length
                            for i in range(len(perm) - 1)
                        ],
                        # corresponding backward order
                        *[
                            self._backward_offsets[perm[i - 1]][mb]
                            >= self._backward_offsets[perm[i]][mb]
                            + self._backward_length
                            for i in range(len(perm) - 1, 0, -1)
                        ],
                        # forward-backward connection order
                        self._backward_offsets[perm[-1]][mb]
                        >= self._forward_offsets[perm[-1]][mb] + self._forward_length,
                    )
                )

            # add all possibilities to z3 constraints
            self._solver.add(z3.Or(*cases))

    def _serial_computation_within_pipeline_constraint(self):
        for pp in range(self._pp_size):
            _pp_vars = self._forward_offsets[pp] + self._backward_offsets[pp]
            for i, _ in enumerate(_pp_vars):
                for j in range(i + 1, len(_pp_vars)):
                    _i_length = (
                        self._forward_length
                        if i // self._num_microbatches == 0
                        else self._backward_length
                    )
                    _j_length = (
                        self._forward_length
                        if j // self._num_microbatches == 0
                        else self._backward_length
                    )
                    self._solver.add(
                        z3.Or(
                            _pp_vars[j] >= _pp_vars[i] + _i_length,
                            _pp_vars[j] + _j_length <= _pp_vars[i],
                        )
                    )

    def _pipeline_activation_accumulation_constraint(self):
        for pp in range(self._pp_size):
            # calculate the maximum activation value for this pp
            for mb in range(self._num_microbatches):
                _backward_var = self._backward_offsets[pp][mb]
                _actvaition_count = 1

                for other_mb in range(self._num_microbatches):
                    if other_mb == mb:
                        continue
                    _actvaition_count += z3.If(
                        z3.And(
                            self._backward_offsets[pp][other_mb] > _backward_var,
                            self._forward_offsets[pp][other_mb] < _backward_var,
                        ),
                        1,
                        0,
                    )

                self._solver.add(_actvaition_count <= self._max_activation_times[pp])

    def _build_constraints(self) -> None:
        for i in range(self._pp_size):
            for mb in range(self._num_microbatches):
                self._forward_offsets[i].append(z3.Int(f"f_{mb}_{i}"))
                self._solver.add(self._forward_offsets[i][-1] >= 0)
                self._backward_offsets[i].append(z3.Int(f"b_{mb}_{i}"))
                self._solver.add(self._backward_offsets[i][-1] >= 0)

        if self._sequential_order_constraint_strategy == "strict":
            # constraint 1-0: forward and backward of each microbatch
            # are executed in sequential order
            self._sequential_order_constraint_strict()
        elif self._sequential_order_constraint_strategy == "double_interleaving":
            # constraint 1-1: forward and backward of each microbatch
            # are executed in sequential order (allowing double interleaving)
            self._sequential_order_constraint_double_interleaving()
        elif self._sequential_order_constraint_strategy == "full_interleaving":
            # constraint 1-2: forward and backward of each microbatch
            # are executed in sequential order (allowing full interleaving)
            self._sequential_order_constraint_full_interleaving()

        # constraint 2: no overlapping of forward and backward within each pipeline
        self._serial_computation_within_pipeline_constraint()

        # constraint 3: the accumulation count of activations does not exceed max_activation_times
        self._pipeline_activation_accumulation_constraint()

    def _build_optimize_objectives(self) -> None:
        # 1. minimize the execution time of each microbatch
        max_var = z3.Int("max_start_offset")

        for pp in range(self._pp_size):
            for var in self._backward_offsets[pp]:
                self._solver.add(max_var >= var)

        self._solver.minimize(max_var)

    def _draw(self, results: dict) -> None:
        painter_conf = {
            "pp_size": self._pp_size,
            "pp_height": 50,
            "pp_align": 10,
            "pixel_base": 10,
            "forward_length": self._forward_length,
            "backward_length": self._backward_length,
        }

        SchedulingPainter(painter_conf).draw(results)

    def run(self) -> None:
        """run simulation"""
        # 1. builds the solver constraints.
        self._build_constraints()

        # 2. builds the solver optimize objectives.
        self._build_optimize_objectives()

        # 3. runs the solver.
        print("Z3 Solver Solving...")
        if self._solver.check() == z3.sat:
            print("Result: SAT")
            # tranforms the result to a dictionary.
            model = self._solver.model()
            results = {str(key): model[key].as_long() for key in model}
            results.pop('max_start_offset')
            # 4. draws the result.
            self._draw(results)
        else:
            print("Result: UNSAT")
