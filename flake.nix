{
  description = "IMAP mail service, Hermes plugin and NixOS module for the Hermes agent";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      nixosModules.default = ./nix/module.nix;

      packages.${system} = {
        default = self.packages.${system}.hermes-mail;
        hermes-mail = pkgs.callPackage ./nix/package.nix { };
        hermes-mail-plugin = pkgs.callPackage ./nix/plugin.nix { };
        # Runs Dovecot and the service in a NixOS VM. It needs KVM, so it is
        # not a check.
        vm-test = import ./nix/tests/vm.nix { inherit pkgs self; };
      };

      checks.${system} = import ./nix/checks.nix { inherit pkgs self nixpkgs; };

      formatter.${system} = pkgs.nixfmt-tree;
    };
}
