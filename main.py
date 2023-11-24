"""
main package
"""
from simulator.simulator import Simulator


def main():
    """main function"""
    config = {
        "pp_size": 4,
        "num_microbatches": 6,
        "forward_execution_time": 2,
        "backward_execution_time": 3,
        # stratiges: "strict", "double_interleaving", "full_interleaving",
        "sequential_order_constraint_strategy": "strict",
        "max_activation_times": [4, 3, 2, 1],
    }

    simulator = Simulator(config)
    simulator.run()


if __name__ == "__main__":
    main()
