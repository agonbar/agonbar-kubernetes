Reference answer (self-test only).

The gke-agent context (and the KUBECONFIG that lists ~/.kube/gke-agent.yaml) is defined in
nix/home/server.nix. flake.nix only gives server.nix to the headless VMs work-vm-01,
kubernetes-10 and kubernetes-20; work-vm-00 uses the desktop profile nix/home/agonbar.nix, so it
gets neither the file nor the variable. Move both the kubeconfig file and KUBECONFIG to a
module both profiles import, e.g. nix/home/modules/kubernetes.nix; the age secret already
reaches both through modules/secrets.nix.
